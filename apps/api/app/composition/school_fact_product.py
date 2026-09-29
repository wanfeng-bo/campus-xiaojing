"""Isolated school-fact product factory; separate from the Hangzhou identity entry."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import ExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from re import fullmatch
from typing import Any, Literal, Protocol, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from redis import Redis

from app.composition.assistant_controls import ControlRedisClient, RedisControlCredentialStore
from app.composition.deepseek_assistant_model import DeepSeekAssistantModel, ModelCallPermit
from app.composition.school_fact_assistant_gateway import OfficialContactToolGateway
from app.composition.school_fact_current_model_budget import SqliteCurrentModelCallPermit
from app.composition.school_fact_dingtalk_identity import (
    build_school_fact_dingtalk_identity_service,
)
from app.composition.school_fact_directory_adapter import FACT_LABELS, DirectoryProductAdapter
from app.composition.school_fact_directory_context import (
    bind_directory_identity,
    current_bound_actor_matches,
    current_directory_actor_matches,
)
from app.composition.school_fact_directory_gateway import (
    DIRECTORY_ASSISTANT_INSTRUCTION,
    CombinedFactTurnGateway,
    DirectoryFactToolGateway,
    OwnReceiptInput,
    ReceiptOriginalQueryWithMetadataProof,
)
from app.composition.school_fact_directory_gateway import (
    MODULE as DIRECTORY_MODULE,
)
from app.composition.school_fact_directory_product import (
    DirectoryFactProductService,
    OwnReceiptTarget,
    ReceiptReferenceFor,
    ReceiptTargetProjection,
    project_own_receipt_target,
)
from app.composition.school_fact_directory_services import create_directory_read_components
from app.composition.school_fact_education_gateway import (
    EDUCATION_ASSISTANT_INSTRUCTION,
    EducationToolGateway,
)
from app.composition.school_fact_education_gateway import (
    MODULE as EDUCATION_MODULE,
)
from app.composition.school_fact_education_maintenance import resolve_family_with_education
from app.composition.school_fact_education_product import EDUCATION_LABELS
from app.composition.school_fact_education_services import create_education_components
from app.composition.school_fact_family_children_gateway import (
    FAMILY_CHILDREN_INSTRUCTION,
    FamilyChildrenToolGateway,
)
from app.composition.school_fact_family_correction_executor import FamilyCorrectionExecutorClient
from app.composition.school_fact_family_correction_product import FamilyCorrectionProduct
from app.composition.school_fact_family_education_query import (
    FamilyEducationQueryService,
    FamilyEducationRepository,
)
from app.composition.school_fact_family_gateway import (
    FAMILY_ASSISTANT_INSTRUCTION,
    FamilyProfileToolGateway,
)
from app.composition.school_fact_family_leadership_gateway import FamilyLeadershipToolGateway
from app.composition.school_fact_family_leadership_query import (
    create_family_leadership_query_service,
)
from app.composition.school_fact_family_quality import (
    FamilyQualityAuthorizer,
    FamilyQualityChangeGuard,
    FamilyQualityCurrentContent,
    FamilyQualityExistingContent,
    FamilyQualityReader,
    SqlFamilyQualityProjection,
)
from app.composition.school_fact_family_quality_product import FamilyQualityProductAdapter
from app.composition.school_fact_family_query import create_family_query_service
from app.composition.school_fact_family_services import create_family_staff_components
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.composition.school_fact_family_staff_gateway import (
    FAMILY_FACT_LABELS,
    FamilyStaffToolGateway,
)
from app.composition.school_fact_family_staff_gateway import MODULE as FAMILY_STAFF_MODULE
from app.composition.school_fact_family_teaching_gateway import (
    FAMILY_TEACHING_INSTRUCTION,
    FamilyTeachingToolGateway,
)
from app.composition.school_fact_family_teaching_query import create_family_teaching_query_service
from app.composition.school_fact_live_settings import LiveSchoolFactProductConfiguration
from app.composition.school_fact_model_budget import SqliteModelCallPermit
from app.composition.school_fact_package_authorization import (
    PackageChangeAuthorization,
    PackageExistingQualityKind,
    PackageFactPartition,
    PackageReferenceAuthorization,
    package_quality_read_proofs,
)
from app.composition.school_fact_package_replacement import PackageReplacementReader
from app.composition.school_fact_package_sources import PackageDomainSourceAdapter
from app.composition.school_fact_product_contacts import (
    ContactProductUnavailable,
    OfficialContactProductService,
)
from app.composition.school_fact_product_identity import (
    ProductFamilyActor,
    ProductIdentity,
    ProductIdentityUnavailable,
    build_school_fact_product_identity_service,
)
from app.composition.school_fact_product_services import create_school_fact_product_services
from app.composition.school_fact_product_settings import (
    ApprovedProductModel,
    ProductConfigurationError,
    SchoolFactProductConfiguration,
    load_product_model_api_key,
    load_school_fact_product_configuration,
    require_product_fact_features,
)
from app.composition.school_fact_query_repository import OfficialContactQueryRepository
from app.composition.school_fact_read_connection import F2ConnectionReadExecutor
from app.composition.school_fact_residence_dependencies import ResidenceEligibilityAdapter
from app.composition.school_fact_residence_gateway import (
    FAMILY_RESIDENCE_INSTRUCTION,
    RESIDENCE_INSTRUCTION,
    FamilyResidenceToolGateway,
    ResidenceToolGateway,
)
from app.composition.school_fact_residence_gateway import (
    MODULE as RESIDENCE_MODULE,
)
from app.composition.school_fact_residence_maintenance import (
    ResidenceLifecycleCoordinator,
    require_partitioned_maintenance,
)
from app.composition.school_fact_residence_preflight import format_residence_preflight_rejection
from app.composition.school_fact_residence_product import RESIDENCE_LABELS, ResidenceProductAdapter
from app.composition.school_fact_residence_quality import (
    ResidenceQualityBridge,
    ResidenceQualityOwnerAuthorizer,
)
from app.composition.school_fact_residence_quality_product import ResidenceQualityProductAdapter
from app.composition.school_fact_residence_services import create_residence_components
from app.composition.school_fact_residence_time import ResidenceTimeService, SqlResidenceSchoolTime
from app.composition.school_fact_residence_views import ResidenceMaintenanceViews
from app.composition.school_fact_staff_teaching_query import (
    SqlStaffTeachingCoordinates,
    StaffTeachingQueryService,
)
from app.composition.school_fact_teaching_gateway import (
    TEACHING_ASSISTANT_INSTRUCTION,
    StaffTeachingGateway,
)
from app.composition.school_facts import (
    InMemoryPrivateSchoolFactSourceStore,
    InMemorySchoolFactPlanStore,
    SchoolFactEphemeralStoreJanitor,
)
from app.core.request_id import assign_request_id
from app.db.connection import (
    build_engine,
    build_session_factory,
    verify_runtime_context_signing_key,
)
from app.interfaces.http.assistant import (
    AssistantRequestIdentity,
    AssistantTurnRunner,
    create_assistant_router,
)
from app.interfaces.http.errors import ApiError, HttpErrorResponse
from app.interfaces.http.framework_request import (
    api_error_handler,
    request_validation_error_handler,
)
from app.interfaces.http.school_fact_dingtalk_identity import (
    SchoolFactDingTalkIdentityHttp,
    install_dingtalk_identity_dependencies,
)
from app.interfaces.http.school_fact_directory import create_directory_product_router
from app.interfaces.http.school_fact_education import create_education_product_router
from app.interfaces.http.school_fact_family import create_family_profile_router
from app.interfaces.http.school_fact_family_corrections import create_family_correction_router
from app.interfaces.http.school_fact_family_staff import create_family_staff_product_router
from app.interfaces.http.school_fact_product_contacts import (
    create_school_fact_product_contact_router,
    create_school_fact_product_receipt_router,
)
from app.interfaces.http.school_fact_product_identity import SchoolFactProductIdentityHttp
from app.interfaces.http.school_fact_residence import (
    create_family_residence_router,
    create_residence_product_router,
)
from app.interfaces.http.school_fact_teaching import create_teaching_read_router
from app.interfaces.http.school_facts import SchoolFactRequestIdentity, create_school_fact_router
from app.platform.assistant.catalog import AssistantModuleCatalogEntry, StaticAssistantCatalog
from app.platform.assistant.controls import AssistantControlService
from app.platform.assistant.runtime import (
    AssistantRuntime,
    AssistantRuntimeError,
    HistoryCheck,
)
from app.platform.assistant.tool_loop import (
    AssistantToolLoop,
    JsonObject,
    ModelReply,
    ModelRequest,
    ModelToolCall,
    OwnerInstruction,
    ToolCallContext,
    ToolExecutionResult,
    ToolGateway,
    ToolModel,
    TurnInstruction,
)
from app.platform.authorization.secure_reader_contracts import (
    SchoolFactReadSelection,
    SecureReadDenied,
    SecureReadRequest,
)
from app.platform.contracts import StaffActorContext
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.session import SessionRedisClient
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import (
    F7_FAMILY_QUALITY_TARGET_KINDS,
    CanonicalFactChangeSetV1,
    CanonicalFactContentV1,
)
from app.platform.school_facts.f2_policy import F2_FACT_KINDS
from app.platform.school_facts.f3_policy import F3_FACT_KINDS
from app.platform.school_facts.ports import SchoolFactOwnResultTarget
from app.platform.school_facts.read_policy import (
    OFFICIAL_CONTACT_CAMPUSES,
    OFFICIAL_CONTACT_MODULE,
    OfficialContactFact,
    SchoolFactAuthorizationUnavailable,
)
from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates


class AssistantLoopFactory(Protocol):
    """Explicit composition override; F2 otherwise uses the supported framework runner."""

    def __call__(
        self,
        *,
        configuration: SchoolFactProductConfiguration | LiveSchoolFactProductConfiguration,
        gateway_factory: Callable[[str], ToolGateway],
        control_service: AssistantControlService,
        instruction: OwnerInstruction,
    ) -> AssistantTurnRunner: ...


class _RedisProbe(Protocol):
    def ping(self) -> object: ...


# Complete approved boundary from design section 1.3; reused by real integration tests.
_F5_PLUS_PURPOSES = frozenset(
    {
        "school-facts-f5-synthetic",
        "school-facts-f6-synthetic",
        "school-facts-f7-synthetic",
        "school-facts-live",
        "school-facts-live-integration-synthetic",
    }
)
_F7_PURPOSES = frozenset({
    "school-facts-f7-synthetic", "school-facts-live",
    "school-facts-live-integration-synthetic",
})


_FACT_ASSISTANT_INSTRUCTION = (
    "你是小静，帮助老师和家长处理校园里的实际问题。\n"
    "【给用户的回答】先回答这次问的事，答完即止。用用户习惯的称呼和日常说法，"
    "简单问题通常一句能说明白；多项问题逐项答全。不要附加用户没问的正常状态、"
    "登记过程、数据结构、内部规则、重复释义或泛泛的服务邀请。维护人员日常提问也遵守"
    "同样原则；只有用户要核对维护依据或技术细节时才展开。\n"
    "工具结果是供你判断的事实、上下文和限制，不是答复草稿。"
    "tool_context供继续调用工具，related_context供理解关联，supporting_evidence供核对出处；"
    "这些材料不因存在就必须讲给用户听。每句话应回答用户所问，提供必要事实，或说明"
    "会改变结论或下一步的必要信息。先用事实形成对原问题的结论，再用用户的说法表达；"
    "不要按材料字段逐项翻译或复述系统如何得到结论。answer_constraints保留影响结论的条件，"
    "supporting_evidence保留各自对象的出处、范围和核对材料；它们都不是答复草稿。"
    "内部用来区分证据的名称不必带给用户；直接解释对他所问事情意味着什么。"
    "用户追问出处、是否齐全或原因时，答清所问的依据与不能确定的部分，不回避问题，"
    "也不把所有证据维度依次汇报。没有独立核对说明，不等于已有事实不可使用或查询失败。"
    "必要限制要让普通老师听懂，紧接它影响的结论说清一次即可，不用审计或接口术语解释。"
    "名单是否齐全不明确时限定所查内容，不能先断言唯一或全部再追加免责声明。\n"
    "【是否继续处理】追问是为了取得完成原任务所缺的必要条件。对象已经确定、所问范围"
    "确实没有当前记录，也可以答复用户；不能仅为找出一个值而重新消歧或换范围。"
    "未核实名单完整性不等于存在一项尚未完成的补查；不许建议一个没有已知可用能力、"
    "或不能消除当前不确定性的查法。仅在原问题确有未完成部分时继续处理或提出具体追问。"
    "联系谁、去哪里办理等办事方式须有正式依据，不从职务或电话号码推断权限。\n"
    "【事实与正式操作】只使用正式工具已核实的事实，逐项对应对象、字段和适用时点。"
    "未查到、未读取、无权限、冲突、失效和来源故障不同；影响答案时如实简短说明。"
    "返回记录不证明完整登记或现实总数，未返回不排除其他记录。来源只支持对应记录实际记载的内容；"
    "查询限制不是来源的正式记载或明确否定。用户问来源时说明原结论实际采用的出处，"
    "未取得就直说，不补猜或把独立质量声明当出处。"
    "修改准备后只需让用户明白对象、实际改动及还需要确认；不要复述未变字段和内部操作"
    "名称。用户指定的未来日期、历史补正及重要联动必须说清；完整差异、来源和生效时间"
    "在正式确认界面核对。必须由用户使用正式控件确认才会执行，不能把待确认说成已修改。"
    "执行成功、拒绝或结果未知以原正式回执为准；读取当前值不能代替原操作成功证明。"
)
_EXCLUDED_SERVICES_INSTRUCTION = (
    "永久排除边界"
    "：本平台不建设任何由人工智能开展的、针对人的心理健康、精神障碍或神经发育状况"
    "的诊断、筛查、评估、咨询、干预、风险预测、心理或相关状况画像、情绪识别、自伤"
    "风险推断，以及自动心理或相关状况预警。这些排除适用于前台对话、后台分析、文件"
    "／量表／行为数据处理和经家长、教职工间接发起的请求；不因改称辅助判断、人工复"
    "核、采用本地模型或取得业务模块授权而重新引入。没有学生端不改变这项边界。普通"
    "人工求助渠道、心理服务信息、生活情绪表达及一般关怀可以正常存在。不把日常表达"
    "转成上述专业服务或分析。人工服务预约和实际转交由明确业务所有者提供受理／送达"
    "结果；显示联系方式或结束聊天不等于完成转交。"
)

F1_ASSISTANT_INSTRUCTION = _FACT_ASSISTANT_INSTRUCTION + (
    "当前仅使用获批合成资料，不宣称真实学校已验证。无需例行解释工程环境或排除条款。"
) + _EXCLUDED_SERVICES_INSTRUCTION
LIVE_ASSISTANT_INSTRUCTION = _FACT_ASSISTANT_INSTRUCTION + (
    "只使用当前学校获准的任务资料。标为仅限页面查看的明细不可猜测或尝试绕过读取。"
    "必要时简短告诉用户在获准资料卡查看；不得声称模型服务商零留存。"
) + _EXCLUDED_SERVICES_INSTRUCTION


class DisabledModel:
    def respond(self, request: ModelRequest) -> ModelReply:
        raise AssistantRuntimeError("The model is unavailable until explicitly configured.")


def shared_own_receipt_projection(
    *,
    configuration: FamilyConfiguration,
    directory_reference_for: ReceiptReferenceFor,
    family_reference_for: ReceiptReferenceFor,
) -> ReceiptTargetProjection:
    # Match the Writer's partition by exact coordinates, including reserved creations.
    # This chooses only presentation; current complete-batch authority stays with Writer.
    family_coordinates = frozenset(
        (kind, key)
        for catalog in (configuration.source_catalog, configuration.creation_catalog)
        for kind, keys in catalog.items()
        for key in keys
    )
    family_exclusive = F3_FACT_KINDS - F2_FACT_KINDS

    def project(actor: StaffActorContext, target: SchoolFactOwnResultTarget) -> OwnReceiptTarget:
        family = (
            target.fact_kind in family_exclusive
            or (target.fact_kind, target.source_key) in family_coordinates
        )
        return project_own_receipt_target(
            actor,
            target,
            reference_for=family_reference_for if family else directory_reference_for,
            labels=FAMILY_FACT_LABELS if family else FACT_LABELS,
            prefix="f3" if family else "f2",
        )

    return project


def shared_own_receipt_tool_aliases(
    directory: DirectoryFactToolGateway | None,
    family: FamilyStaffToolGateway | None,
    education: EducationToolGateway | None,
) -> frozenset[str]:
    """Advertise one receipt pair only for this verified complete product assembly."""
    if directory is None or family is None or education is None:
        return frozenset()
    products = (directory.product, family.product, education.product)
    boundary = getattr(directory.product, "_boundary", None)
    projection = getattr(directory.product, "_receipt_target_projection", None)
    if (
        boundary is None
        or not callable(projection)
        or any(getattr(product, "_boundary", None) is not boundary for product in products)
        or any(
            getattr(product, "_receipt_target_projection", None) is not projection
            for product in products
        )
        or family.resolve_actor is not directory.resolve_actor
        or education.resolve_actor is not directory.resolve_actor
    ):
        return frozenset()
    # These paths all read the Writer's aggregate own scope and shared projection.
    # Per-owner receipt/prepare tools still use separate private action registries.
    return frozenset(
        module + "." + action
        for module in (FAMILY_STAFF_MODULE, EDUCATION_MODULE)
        for action in ("own_receipt", "own_receipts")
    )


def read_receipt_current_teaching(
    teaching: StaffTeachingGateway, target: OwnReceiptTarget, context: ToolCallContext
) -> ToolExecutionResult:
    """Follow an authorized receipt target using the installed exact teaching read."""
    if (
        target.fact_kind not in {"teaching_assignment", "teaching_offering"}
        or (fullmatch(rf"f4:{target.fact_kind}:[0-9a-f]{{64}}", target.reference) is None
            and school_scope_reference_coordinates(target.reference)
            != ("f5", target.fact_kind, False))
    ):
        return ToolExecutionResult(model_content={"status": "unsupported"})
    return teaching.execute(
        ModelToolCall(
            call_id="receipt-current",
            name="school_facts.teaching.query",
            arguments={"view": "teaching", "target_reference": target.reference},
        ),
        context,
    )


def read_receipt_current_residence(residence, retained, target, context):
    """Dispatch an already authorized exact receipt target to its installed owner."""
    prefixes = {"fact_quality": "f2", "student_school_relationship": "f3"}
    if target.fact_kind not in RESIDENCE_LABELS and target.fact_kind not in prefixes:
        return (
            retained(target, context)
            if retained is not None
            else ToolExecutionResult(model_content={"status": "unsupported"})
        )
    prefix = prefixes.get(target.fact_kind, "f6")
    if (fullmatch(rf"{prefix}:{target.fact_kind}:[0-9a-f]{{64}}", target.reference) is None
            and school_scope_reference_coordinates(target.reference)
            != (prefix, target.fact_kind, False)):
        return ToolExecutionResult(model_content={"status": "unsupported"})
    return residence.execute(
        ModelToolCall(
            call_id="receipt-current",
            name=RESIDENCE_MODULE + ".query",
            arguments={"fact_kind": target.fact_kind, "reference": target.reference},
        ),
        context,
    )


def read_receipt_original_teaching(
    directory: DirectoryFactToolGateway,
    teaching: StaffTeachingGateway,
    view: JsonObject,
    selection: OwnReceiptInput,
    context: ToolCallContext,
) -> ToolExecutionResult:
    """Project one receipt's exact original versions, never a historical time guess."""
    from app.composition.school_fact_education_authority import EducationAuthority, EducationPermit
    from app.composition.school_fact_read_evidence import validate_fallback_read, validation_batch
    from app.composition.school_fact_receipt_teaching import (
        OriginalTeachingNameProof,
        project_original_teaching,
    )

    identity = teaching.identity(context.actor_context_ref)
    expected_view = deepcopy(view)
    offset, limit = selection.target_offset, min(selection.target_limit, 3)

    def read():
        fresh = teaching.identity(context.actor_context_ref)
        if fresh.actor != identity.actor or fresh.session_binding != identity.session_binding:
            raise PermissionError("Original operation identity changed")
        receipt, original = directory.product.own_receipt_original(
            actor=fresh.actor,
            session_binding=fresh.session_binding,
            receipt_ref=selection.receipt_ref,
            request_id=context.request_id,
            target_offset=offset,
            target_limit=limit,
        )
        if receipt.model_dump(mode="json") != expected_view:
            raise AssistantRuntimeError("Original operation metadata changed")
        return original

    original = read()
    authority = cast(EducationAuthority, teaching.query.repository.authority)
    permits: list[EducationPermit] = []
    proofs: list[OriginalTeachingNameProof] = []
    results: list[Any] = []
    targets = cast(list[dict[str, Any]], view["targets"])[offset : offset + limit]
    for projected, target in zip(targets, original.targets[offset : offset + limit], strict=True):
        fields = target.original_result
        if fields is None:
            material = {"status": "unsupported"}
        elif fields.status != "available":
            material = {"status": fields.status}
        else:
            permit = authority.require_scope(
                identity.actor,
                fact_kind=target.fact_kind,
                action="read",
                checked_at=teaching.query.repository.clock(),
            )
            authority.require_object(permit, target.source_key, is_creation=False)
            authority.require_fields(permit, fields.field_names)
            permits.append(permit)
            projected_fields = project_original_teaching(
                identity.actor,
                fields.segments,
                external_reader=teaching.query.external_reader,
                clock=teaching.query.repository.clock,
            )
            proofs.append(projected_fields.proof)
            material = {
                "status": "available",
                **projected_fields.material,
                "selected_fields_changed": fields.field_changes,
                "scope": "仅本次正式修改实际影响时段的任教安排，不代表全部教师或后续变更过程。",
                "source": "原正式修改记录",
                "ended_intervals": [
                    {
                        "effective_at": segment.effective_at.isoformat(),
                        "valid_to": None
                        if segment.valid_to is None
                        else segment.valid_to.isoformat(),
                    }
                    for segment in fields.before_segments
                ]
                if fields.field_changes and not fields.segments
                else [],
            }
        results.append(
            {
                "tool_context": {
                    "reference": projected["reference"],
                    "fact_kind": target.fact_kind,
                },
                "result": material,
            }
        )

    expected = deepcopy(original)
    snapshot_key = object()

    def current() -> bool:
        with bind_directory_identity(context.actor_context_ref), validation_batch():
            # The exact immutable original is checked once at this synchronous
            # release boundary, including a fresh Writer authorization. Nothing
            # survives a model wait or the next boundary.
            validate_fallback_read(snapshot_key, lambda: read() == expected)
            for proof in proofs:
                proof.revalidate(actor=identity.actor)
            authority.revalidate_many(
                tuple(permits),
                actor=identity.actor,
                checked_at=teaching.query.repository.clock(),
            )
            fresh = teaching.identity(context.actor_context_ref)
            return (
                fresh.actor == identity.actor and fresh.session_binding == identity.session_binding
            )

    check = HistoryCheck(current, revalidate=current, revalidation_covers_content=True)
    if not check.valid():
        raise AssistantRuntimeError("Original operation fields changed")
    return ToolExecutionResult(model_content={"original_results": results}, history_check=check)


def build_product_model(
    configuration: ApprovedProductModel | None, *, parallel_tool_calls: Literal[False] | None = None
) -> ToolModel:
    if configuration is None:
        return DisabledModel()
    try:
        api_key = load_product_model_api_key(configuration)
        # The cumulative budget is durable metadata, never subject to chat expiration.
        permit: ModelCallPermit
        if configuration.model == "deepseek-flash":
            if configuration.current_approval is None:
                raise ValueError("current model approval is required")
            permit = SqliteCurrentModelCallPermit(
                path=configuration.state_directory / "model-budget.sqlite3",
                ledger_policy=configuration.budget,
                current_approval=configuration.current_approval,
            )
        else:
            permit = SqliteModelCallPermit(
                path=configuration.state_directory / "model-budget.sqlite3",
                policy=configuration.budget,
            )
        return DeepSeekAssistantModel(
            api_key=api_key,
            model=configuration.model,
            permit=permit,
            current_approval=configuration.current_approval,
            max_input_tokens=configuration.max_input_tokens,
            max_output_tokens=configuration.max_output_tokens,
            parallel_tool_calls=parallel_tool_calls,
        )
    except Exception:
        pass
    # Raise outside the handler: hidden exception contexts must not retain key bytes.
    raise ProductConfigurationError("F1 model configuration is unavailable.")


@dataclass(frozen=True, slots=True)
class StaffProductIdentity:
    actor: StaffActorContext
    session_binding: str
    actor_context_ref: str


def require_staff_product_identity(identity: ProductIdentity) -> StaffProductIdentity:
    if type(identity.actor) is not StaffActorContext:
        raise ApiError(403, "identity.staff_required", "请先选择有权办理此事项的履职身份。")
    return StaffProductIdentity(
        identity.actor, identity.session_binding, identity.actor_context_ref
    )


@dataclass(frozen=True, slots=True)
class _ContactTarget:
    source_key: str
    school_fact_version: int
    version: int
    content: CanonicalFactContentV1
    public_fact: OfficialContactFact


def resolve_contact_target(
    *, query: OfficialContactQueryRepository, actor: StaffActorContext, fact_ref: str
) -> _ContactTarget:
    try:
        row = query.resolve_maintenance_target(actor=actor, fact_ref=fact_ref)
    except SecureReadDenied:
        raise PermissionError("Current maintenance projection is not permitted.") from None
    public = query.read_official_contacts(actor=actor, campus_ref=f"campus:{row.campus_id}")
    if public.status == "forbidden":
        raise PermissionError("Current public contact projection is not permitted.")
    if public.status == "source_unavailable":
        raise SchoolFactAuthorizationUnavailable("The current fact source is unavailable.")
    matches = tuple(fact for fact in public.contacts if fact.fact_ref == fact_ref)
    if public.status != "known" or len(matches) != 1:
        raise ContactProductUnavailable("The current public contact is unavailable.")
    fact = matches[0]
    if (
        fact.version != row.expected_version
        or fact.campus_ref != f"campus:{row.campus_id}"
        or (
            fact.contact_kind,
            fact.contact_value,
            fact.service_hours,
            fact.valid_from,
            fact.valid_to,
        )
        != (row.contact_kind, row.contact_value, row.service_hours, row.valid_from, row.valid_to)
        or row.owner_kind != "location"
    ):
        raise ContactProductUnavailable("The contact changed while preparing; query it again.")
    return _ContactTarget(
        source_key=row.source_key,
        school_fact_version=row.school_fact_version,
        version=row.expected_version,
        public_fact=fact,
        content=CanonicalFactContentV1(
            owner_kind=row.owner_kind,
            location_source_key=row.campus_source_key,
            contact_kind=row.contact_kind,
            contact_value=row.contact_value,
            service_hours=row.service_hours,
            lifecycle_status=row.lifecycle_status,
            visibility_scope=row.visibility_scope,
            audience_code=row.audience_code,
        ),
    )


def create_school_fact_product_app(
    *, assistant_loop_factory: AssistantLoopFactory | None = None,
    configuration: (
        SchoolFactProductConfiguration | LiveSchoolFactProductConfiguration | None
    ) = None,
) -> FastAPI:
    if configuration is None:
        configuration = load_school_fact_product_configuration()
    is_live = isinstance(configuration, LiveSchoolFactProductConfiguration)
    assistant_disabled = is_live and not configuration.assistant_enabled
    settings = configuration.runtime
    resources = ExitStack()
    try:
        engine = build_engine(settings)
        resources.callback(engine.dispose)
        verify_runtime_context_signing_key(engine, settings)
        sessions = build_session_factory(engine)
        services = create_school_fact_product_services(
            reader_settings=configuration.reader,
            executor_settings=configuration.executor,
            scope_id=configuration.scope_id,
        )
        resources.callback(services.close)
        redis = Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_read_timeout_seconds,
            retry_on_timeout=False,
        )
        resources.callback(redis.close)
        cast(_RedisProbe, redis).ping()

        def require_maintenance(actor: StaffActorContext) -> object:
            try:
                return services.maintenance_guard.require_current(actor)
            except SchoolFactChangeRejected:
                raise PermissionError(
                    "Current official contact maintenance duty is absent."
                ) from None

        def can_maintain(actor: StaffActorContext) -> bool:
            # This flag describes the F1 contact controls; F2 checks each exact object.
            if configuration.directory is not None:
                return False
            try:
                require_maintenance(actor)
                return True
            except PermissionError:
                return False
            except SchoolFactAuthorizationUnavailable as exc:
                raise ProductIdentityUnavailable(
                    "Current maintenance authority is unavailable."
                ) from exc

        def require_read(actor: StaffActorContext) -> None:
            # Even an empty page is an authoritative, permission-checked result.
            services.secure_reader.read(
                SecureReadRequest(
                    actor=actor,
                    operation_code=OFFICIAL_CONTACT_CAMPUSES,
                    page_size=1,
                    selection=SchoolFactReadSelection(),
                )
            )

        if is_live:
            identity = build_school_fact_dingtalk_identity_service(
                settings=settings,
                session_factory=sessions,
                redis=cast(SessionRedisClient, redis),
                proof_signer=ContextProofSigner.from_settings(settings),
                can_maintain=can_maintain,
                family_enabled=configuration.family is not None and configuration.family.enabled,
            )
            identity_http = SchoolFactDingTalkIdentityHttp(
                service=identity,
                settings=settings,
                directory_enabled=configuration.directory is not None,
                education_enabled=configuration.education is not None
                and configuration.education.enabled,
                allowed_origin=configuration.allowed_origin,
                trusted_proxy_cidrs=configuration.trusted_proxy_cidrs,
            )
        else:
            identity = build_school_fact_product_identity_service(
                app_env=settings.app_env,
                session_factory=sessions,
                redis=cast(SessionRedisClient, redis),
                proof_signer=ContextProofSigner.from_settings(settings),
                can_maintain=can_maintain,
                family_enabled=configuration.family is not None and configuration.family.enabled,
            )
            identity_http = SchoolFactProductIdentityHttp(
                service=identity,
                directory_enabled=configuration.directory is not None,
                education_enabled=configuration.education is not None
                and configuration.education.enabled,
                allowed_origin=configuration.allowed_origin,
                trusted_proxy_cidrs=configuration.trusted_proxy_cidrs,
            )

        def resolve_staff_actor(ref: str) -> StaffProductIdentity:
            return require_staff_product_identity(identity.resolve_private_actor(ref))

        def staff_http_identity(request: Request) -> StaffProductIdentity:
            return require_staff_product_identity(identity_http.request_dependency(request))

        def target_resolver(*, actor: StaffActorContext, fact_ref: str) -> _ContactTarget:
            return resolve_contact_target(
                query=services.query_repository, actor=actor, fact_ref=fact_ref
            )

        def feature_guard(
            delegate: Callable[[StaffActorContext, CanonicalFactChangeSetV1], object],
        ):
            def checked(actor: StaffActorContext, change_set: CanonicalFactChangeSetV1):
                require_product_fact_features(configuration, change_set)
                return delegate(actor, change_set)

            return checked

        plan_store = InMemorySchoolFactPlanStore()
        contacts = OfficialContactProductService(
            boundary=services.writer_boundary,
            target_resolver=target_resolver,
            require_maintenance=require_maintenance,
            plan_store=plan_store,
        )
        retention_seconds = (
            configuration.model.chat_retention_hours * 3600
            if configuration.model is not None
            else 60
        )
        # This TTL expires only conversational recent-reference hints. Formal Writer receipts,
        # recovery records, facts, audit and the model budget are durable and are never swept here.
        gateway = OfficialContactToolGateway(
            resolve_actor=resolve_staff_actor,
            require_read=require_read,
            query=services.query_repository,
            contacts=contacts,
            receipt_retention_seconds=retention_seconds,
        )
        directory_components = None
        directory_adapter = None
        directory_product = None
        directory_gateway = None
        shared_receipt_projection: ReceiptTargetProjection | None = None

        def project_shared_receipt(
            actor: StaffActorContext, target: SchoolFactOwnResultTarget
        ) -> OwnReceiptTarget:
            if shared_receipt_projection is None:
                raise ContactProductUnavailable("receipt projection unavailable")
            return shared_receipt_projection(actor, target)

        if configuration.directory is not None:
            directory_components = create_directory_read_components(
                sessions=services.reader_sessions,
                settings=configuration.reader,
                configuration=configuration.directory,
                legacy_scope_id=configuration.scope_id,
                reference_key=configuration.control_signing_key.get_secret_value().encode(),
                self_person_validator=lambda actor, _now: current_directory_actor_matches(
                    actor, resolve_staff_actor
                ),
            )
            directory_adapter = DirectoryProductAdapter(
                directory_components.query, evidence=directory_components.evidence
            )
            f2_guard = directory_components.guard
            family_quality_adapter = None
            family_quality_changes = None
            family_quality_existing = None
            existing_quality_kind = None

            def resolve_directory_change(**kwargs):
                payload = kwargs["payload"]
                if family_quality_adapter is not None and family_quality_adapter.recognizes(
                    kwargs["actor"], payload
                ):
                    return family_quality_adapter.resolve_change(**kwargs)
                return directory_adapter.resolve_change(**kwargs)

            def require_f2_maintenance(
                actor: StaffActorContext, change_set: CanonicalFactChangeSetV1
            ) -> None:
                try:
                    if family_quality_changes is not None and all(
                        c.fact_kind == "fact_quality"
                        and (
                            getattr(c.content, "target_fact_kind", None)
                            in F7_FAMILY_QUALITY_TARGET_KINDS
                            or c.expected_version > 0
                            and existing_quality_kind(actor, c.source_key)
                            in F7_FAMILY_QUALITY_TARGET_KINDS
                        )
                        for c in change_set.changes
                    ):
                        family_quality_changes.require_change_set(actor, change_set)
                        return
                    f2_guard.require_change_set(actor, change_set)
                except SchoolFactChangeRejected:
                    raise PermissionError("当前身份未获准此事实范围的维护。") from None

            def require_f2_receipt(actor: StaffActorContext) -> None:
                for kind in F2_FACT_KINDS:
                    try:
                        f2_guard.require_current(actor, kind)
                        return
                    except SchoolFactChangeRejected:
                        continue
                raise PermissionError("当前身份未获准事实维护回执。")

            directory_product = DirectoryFactProductService(
                boundary=services.writer_boundary,
                resolve_change=resolve_directory_change,
                require_maintenance=feature_guard(require_f2_maintenance),
                require_receipt=require_f2_receipt,
                plan_store=plan_store,
                receipt_reference_for=directory_components.query.reference_for,
                receipt_fact_labels=FACT_LABELS,
                receipt_target_projection=(
                    project_shared_receipt
                    if configuration.family is not None and configuration.family.enabled
                    else None
                ),
            )
            directory_gateway = DirectoryFactToolGateway(
                resolve_actor=resolve_staff_actor,
                query=directory_adapter,
                product=directory_product,
                retention_seconds=retention_seconds,
            )
        family_education = None
        family_query = None
        family_gateway = None
        family_staff = None
        family_product = None
        family_staff_gateway = None
        family_correction_gateway = None
        family_corrections = None
        education_components = None
        education_gateway = None
        education_product = None
        teaching_gateway = None
        family_teaching_gateway = None
        leadership_gateway = None
        family_teaching = None
        family_leadership = None
        teaching_query = None
        residence_components = None
        residence_product = None
        residence_gateway = None
        family_residence_gateway = None
        residence_lifecycle = None
        residence_views = None
        residence_dependencies = None

        def resolve_family_change(**kwargs):
            result = resolve_family_with_education(
                maintenance=education_components.maintenance
                if education_components is not None and configuration.purpose in _F5_PLUS_PURPOSES
                else None,
                fallback=family_staff.adapter.resolve_change,
                **kwargs,
            )
            return (
                residence_lifecycle.expand(actor=kwargs["actor"], resolved=result)
                if residence_lifecycle is not None
                else result
            )

        def require_family_change(actor, change_set):
            retained = (
                education_components.require_maintenance
                if education_components is not None and change_set.schema_version in {3, 4}
                else family_staff.require_maintenance
            )
            if residence_components is not None:
                return require_partitioned_maintenance(
                    actor, change_set, residence=residence_components, retained=retained,
                    dependency=residence_dependencies.require_maintenance
                    if residence_dependencies is not None else None,
                )
            return retained(actor, change_set)

        def resolve_education_change(**kwargs):
            result = education_components.maintenance.resolve_change(**kwargs)
            return (
                residence_lifecycle.expand(actor=kwargs["actor"], resolved=result)
                if residence_lifecycle is not None
                else result
            )

        def require_education_change(actor, change_set):
            if residence_components is not None:
                return require_partitioned_maintenance(
                    actor,
                    change_set,
                    residence=residence_components,
                    retained=education_components.require_maintenance,
                    dependency=residence_dependencies.require_maintenance
                    if residence_dependencies is not None else None,
                )
            return education_components.require_maintenance(actor, change_set)

        if configuration.family is not None and configuration.family.enabled:
            family_query = create_family_query_service(
                session_factory=services.reader_sessions,
                current_actor=lambda actor: current_bound_actor_matches(
                    actor, identity.resolve_private_actor
                ),
                enable_quality=(
                    configuration.reader.schema_contract
                    in {"school-facts-f7-v55", "school-facts-f7-v56", "school-facts-live-v57"}
                    and configuration.f7_packages_enabled
                ),
            )
            family_education = (
                FamilyEducationQueryService(
                    repository=FamilyEducationRepository(services.reader_sessions),
                    current_actor=lambda actor: current_bound_actor_matches(
                        actor, identity.resolve_private_actor
                    ),
                )
                if configuration.education is not None and configuration.education.enabled
                else None
            )
            family_gateway = FamilyProfileToolGateway(
                resolve_actor=identity.resolve_private_actor,
                query=family_query,
                education=family_education,
            )
            if configuration.purpose in _F5_PLUS_PURPOSES and configuration.f5_features_enabled:
                family_teaching = create_family_teaching_query_service(
                    session_factory=services.reader_sessions,
                    current_actor=lambda actor: current_bound_actor_matches(
                        actor, identity.resolve_private_actor
                    ),
                )
                family_teaching_gateway = FamilyTeachingToolGateway(
                    resolve_actor=identity.resolve_private_actor,
                    query=family_teaching,
                )
                if configuration.family_leadership is not None:
                    family_leadership = create_family_leadership_query_service(
                        session_factory=services.reader_sessions,
                        current_actor=lambda actor: current_bound_actor_matches(
                            actor, identity.resolve_private_actor
                        ),
                        policy=lambda: configuration.family_leadership,
                    )
                    leadership_gateway = FamilyLeadershipToolGateway(
                        resolve_actor=identity.resolve_private_actor,
                        query=family_leadership,
                    )
            if directory_components is None:
                raise ProductConfigurationError("F3 requires the retained directory components.")
            family_staff = create_family_staff_components(
                sessions=services.reader_sessions,
                configuration=configuration.family,
                reference_key=configuration.control_signing_key.get_secret_value().encode(),
                directory=directory_components,
            )
            if configuration.reader.schema_contract in {
                "school-facts-f7-v47",
                "school-facts-f7-v48",
                "school-facts-f7-v49",
                "school-facts-f7-v50",
                "school-facts-f7-v51",
                "school-facts-f7-v52",
                "school-facts-f7-v53",
                "school-facts-f7-v54",
                "school-facts-f7-v55",
                "school-facts-f7-v56", "school-facts-live-v57",
            }:
                family_quality_authorizer = FamilyQualityAuthorizer(
                    f2_guard, family_staff.authority
                )
                family_quality_existing = FamilyQualityExistingContent(
                    f2_guard=f2_guard,
                    authorizer=family_quality_authorizer,
                    projection=SqlFamilyQualityProjection(services.reader_sessions),
                    clock=lambda: datetime.now(UTC),
                )
                existing_quality_kind = (
                    PackageExistingQualityKind(
                        f2_guard=f2_guard, sessions=services.reader_sessions
                    ).target_kind
                    if configuration.reader.schema_contract in {
                        "school-facts-f7-v54",
                        "school-facts-f7-v55",
                        "school-facts-f7-v56", "school-facts-live-v57",
                    }
                    else family_quality_existing.target_kind
                )
                family_quality_changes = FamilyQualityChangeGuard(
                    family_quality_authorizer,
                    clock=lambda: datetime.now(UTC),
                    existing_content=family_quality_existing,
                )
                family_quality_adapter = FamilyQualityProductAdapter(
                    directory=directory_adapter,
                    family=family_staff,
                    guard=family_quality_changes,
                    existing=family_quality_existing,
                    existing_target_kind=existing_quality_kind,
                )
                family_staff.query.quality_reader = FamilyQualityReader(
                    authorizer=family_quality_authorizer,
                    executor=F2ConnectionReadExecutor(
                        services.reader_sessions,
                        family_staff.authority.connection_authority,
                    ),
                    current_version=family_quality_existing.projection.current_version,
                    clock=lambda: datetime.now(UTC),
                )
            correction_client = FamilyCorrectionExecutorClient(settings=configuration.executor)
            resources.callback(correction_client.close)

            def education_correction_source(actor, kind, reference):
                if education_components is None:
                    raise ValueError("Education unavailable")
                return education_components.repository.source_for_reference(actor, kind, reference)

            def correction_staff_source(actor, reference):
                live_coordinates = school_scope_reference_coordinates(reference)
                if live_coordinates and live_coordinates[2]:
                    raise ValueError("A correction must refer to an existing fact")
                if reference.startswith("f6:") or live_coordinates and live_coordinates[0] == "f6":
                    parts = reference.split(":", 2)
                    if len(parts) != 3 or residence_components is None:
                        raise ValueError("Residence reference unavailable")
                    kind = live_coordinates[1] if live_coordinates else parts[1]
                    return kind, residence_components.repository.source_for_reference(
                        actor, kind, reference
                    )
                if reference.startswith("f2:") or live_coordinates and live_coordinates[0] == "f2":
                    parts = reference.split(":", 2)
                    if len(parts) != 3 or directory_components is None:
                        raise ValueError("Directory reference unavailable")
                    kind = live_coordinates[1] if live_coordinates else parts[1]
                    return kind, directory_components.query.source_for(
                        actor=actor, fact_kind=kind, reference=reference
                    )
                return family_staff.repository.source_for_reference(actor, reference)

            family_corrections = FamilyCorrectionProduct(
                executor=correction_client,
                current_actor=lambda actor: current_bound_actor_matches(
                    actor, identity.resolve_private_actor
                ),
                staff_source=correction_staff_source,
                education_source=education_correction_source,
            )
            shared_receipt_projection = shared_own_receipt_projection(
                configuration=configuration.family,
                directory_reference_for=directory_components.query.reference_for,
                family_reference_for=family_staff.repository.reference_for,
            )
            family_product = DirectoryFactProductService(
                preflight_rejection_formatter=format_residence_preflight_rejection,
                boundary=services.writer_boundary,
                resolve_change=resolve_family_change,
                require_maintenance=feature_guard(require_family_change),
                require_receipt=family_staff.require_receipt,
                plan_store=plan_store,
                receipt_reference_for=family_staff.repository.reference_for,
                receipt_fact_labels=FAMILY_FACT_LABELS,
                receipt_reference_prefix="f3",
                receipt_uses_current_targets=True,
                receipt_target_projection=project_shared_receipt,
            )
            family_staff_gateway = FamilyStaffToolGateway(
                resolve_actor=resolve_staff_actor,
                query=family_staff.query,
                product=family_product,
                retention_seconds=retention_seconds,
            )
        if configuration.education is not None and configuration.education.enabled:
            if family_staff is None or directory_components is None or directory_adapter is None:
                raise ProductConfigurationError(
                    "Education requires the existing family and directory boundaries."
                )
            education_components = create_education_components(
                sessions=services.reader_sessions,
                reader_settings=configuration.reader,
                legacy_scope_id=configuration.scope_id,
                configuration=configuration.education,
                reference_key=configuration.control_signing_key.get_secret_value().encode(),
                directory=directory_components,
                directory_adapter=directory_adapter,
                family=family_staff,
            )
            retained_receipt_projection = shared_receipt_projection
            dependency_receipt_projection = shared_own_receipt_projection(
                configuration=education_components.family.configuration,
                directory_reference_for=education_components.dependency_directory.query.reference_for,
                family_reference_for=education_components.family.repository.reference_for,
            )
            dependency_coordinates = frozenset(
                (kind, key)
                for component in (
                    education_components.family.configuration,
                    configuration.education.dependency_directory,
                )
                if component is not None
                for catalog in (
                    getattr(component, "source_catalog", {}),
                    component.creation_catalog,
                )
                for kind, keys in catalog.items()
                for key in keys
            )

            def education_receipt_projection(actor, target):
                if target.fact_kind in EDUCATION_LABELS:
                    from app.composition.school_fact_education_authority import (
                        EducationAuthorityDenied,
                    )

                    try:
                        return project_own_receipt_target(
                            actor,
                            target,
                            reference_for=education_components.repository.reference_for,
                            labels=EDUCATION_LABELS,
                            prefix="f4",
                        )
                    except EducationAuthorityDenied:
                        if not target.creation_receipt:
                            raise
                        # Writer has already verified the owner's original creation command.
                        # This audit-only identifier cannot select a current fact or new object.
                        return project_own_receipt_target(
                            actor,
                            target,
                            reference_for=education_components.repository.creation_receipt_reference_for,
                            labels=EDUCATION_LABELS,
                            prefix="f4audit",
                        )
                if (target.fact_kind, target.source_key) in dependency_coordinates:
                    return dependency_receipt_projection(actor, target)
                if retained_receipt_projection is None:
                    raise ContactProductUnavailable("receipt inaccessible")
                return retained_receipt_projection(actor, target)

            shared_receipt_projection = education_receipt_projection
            education_product = DirectoryFactProductService(
                preflight_rejection_formatter=format_residence_preflight_rejection,
                boundary=services.writer_boundary,
                resolve_change=resolve_education_change,
                require_maintenance=feature_guard(require_education_change),
                require_receipt=education_components.require_receipt,
                plan_store=plan_store,
                receipt_reference_for=education_components.repository.reference_for,
                receipt_fact_labels=EDUCATION_LABELS,
                receipt_reference_prefix="f4",
                receipt_uses_current_targets=True,
                receipt_target_projection=project_shared_receipt,
            )
            education_gateway = EducationToolGateway(
                resolve_actor=resolve_staff_actor,
                query=education_components.query,
                describe_adapter=education_components.views,
                product=education_product,
                retention_seconds=retention_seconds,
                teaching_enabled=configuration.purpose in _F5_PLUS_PURPOSES
                and configuration.f5_features_enabled,
            )
            if configuration.purpose in _F5_PLUS_PURPOSES and configuration.f5_features_enabled:
                teaching_query = StaffTeachingQueryService(
                    education_components.repository,
                    external_reader=education_components.query.external_reader,
                    identity_coordinates=SqlStaffTeachingCoordinates(services.reader_sessions),
                    class_query=education_components.query,
                )
                teaching_gateway = StaffTeachingGateway(
                    resolve_actor=resolve_staff_actor, query=teaching_query
                )
                if directory_gateway is not None:
                    directory_gateway.receipt_current_query = partial(
                        read_receipt_current_teaching, teaching_gateway
                    )
                    directory_gateway.receipt_original_query = (
                        ReceiptOriginalQueryWithMetadataProof(
                            partial(
                                read_receipt_original_teaching, directory_gateway, teaching_gateway
                            )
                        )
                    )
        if (
            configuration.residence is not None
            and configuration.residence.enabled
            and configuration.f6_features_enabled
        ):
            if education_components is None or family_staff is None:
                raise ProductConfigurationError(
                    "F6 requires retained family and education components."
                )
            if configuration.residence.dependency_education is None:
                raise ProductConfigurationError("F6 requires its exact independent dependencies.")
            residence_dependencies = create_education_components(
                sessions=services.reader_sessions,
                reader_settings=configuration.reader,
                legacy_scope_id=configuration.scope_id,
                configuration=configuration.residence.dependency_education,
                reference_key=configuration.control_signing_key.get_secret_value().encode(),
                directory=directory_components,
                directory_adapter=directory_adapter,
                family=family_staff,
            )
            residence_dependencies.maintenance.canonical_version = 4
            residence_directory = residence_dependencies.dependency_directory

            def resolve_residence_related(actor, reference):
                coordinates = school_scope_reference_coordinates(reference)
                if coordinates and coordinates[2]:
                    raise PermissionError("Existing residence related reference required")
                if reference.startswith("f3:") or coordinates and coordinates[0] == "f3":
                    return residence_dependencies.family.repository.source_for_reference(
                        actor, reference
                    )
                parts = reference.split(":", 2)
                if ((len(parts) != 3 or parts[0] != "f2" or parts[1] != "location")
                        and coordinates != ("f2", "location", False)):
                    raise PermissionError("Unsupported residence related reference")
                return "location", residence_directory.query.source_for(
                    actor=actor, fact_kind="location", reference=reference
                )

            residence_time = ResidenceTimeService(
                load=SqlResidenceSchoolTime(
                    sessions=sessions,
                    resolve_identity=identity.resolve_private_actor,
                    proof_signer=ContextProofSigner.from_settings(settings),
                ),
                current_actor=lambda actor: current_bound_actor_matches(
                    actor, identity.resolve_private_actor
                ),
            )
            residence_components = create_residence_components(
                sessions=services.reader_sessions,
                configuration=configuration.residence,
                reference_key=configuration.control_signing_key.get_secret_value().encode(),
                current_actor=lambda actor: current_bound_actor_matches(
                    actor, identity.resolve_private_actor
                ),
                resolve_related=resolve_residence_related,
                related_reference_for=lambda actor, kind, key: (
                    residence_dependencies.query.external_reader.reference_for(
                        actor=actor, fact_kind=kind, source_key=key
                    )
                ),
                time_context=residence_time,
            )
            residence_adapter = ResidenceProductAdapter(
                repository=residence_components.repository,
                external=residence_dependencies.query.external_reader,
                time_context=residence_time,
            )
            residence_quality = ResidenceQualityBridge(
                f2_guard=residence_directory.guard,
                retained_f2_guard=directory_components.guard,
                residence_authority=residence_components.authority,
                projection=residence_components.repository.projection,
                owner_authorizer=ResidenceQualityOwnerAuthorizer(
                    f2_guard=residence_directory.guard,
                    family_authority=residence_dependencies.family.authority,
                    clock=residence_components.repository.clock,
                ),
            )
            quality_adapter = ResidenceQualityProductAdapter(
                bridge=residence_quality,
                cursor_codec=residence_components.query.cursor_codec,
                directory=DirectoryProductAdapter(
                    residence_directory.query, evidence=residence_directory.evidence
                ),
                residence=residence_components.repository,
                external=residence_dependencies.query.external_reader,
            )
            residence_lifecycle = ResidenceLifecycleCoordinator(
                repository=residence_components.repository,
                adapter=residence_adapter,
                family_repository=residence_dependencies.family.repository,
            )
            residence_eligibility = ResidenceEligibilityAdapter(
                education=residence_dependencies, lifecycle=residence_lifecycle
            )
            residence_views = ResidenceMaintenanceViews(
                repository=residence_components.repository,
                query=residence_components.query,
                adapter=residence_adapter,
                external_choices=residence_dependencies.views.query,
                quality=quality_adapter,
                eligibility=residence_eligibility,
            )
            retained_residence_projection = shared_receipt_projection
            residence_dependency_projection = shared_own_receipt_projection(
                configuration=residence_dependencies.family.configuration,
                directory_reference_for=residence_directory.query.reference_for,
                family_reference_for=residence_dependencies.family.repository.reference_for,
            )
            residence_dependency_coordinates = frozenset(
                (kind, key)
                for component in (
                    residence_dependencies.family.configuration,
                    configuration.residence.dependency_education.dependency_directory,
                    configuration.residence.dependency_education,
                )
                if component is not None
                for catalog in (
                    getattr(component, "source_catalog", {}),
                    component.creation_catalog,
                )
                for kind, keys in catalog.items()
                for key in keys
            )

            def residence_receipt_projection(actor, target):
                if target.fact_kind in RESIDENCE_LABELS:
                    try:
                        return project_own_receipt_target(
                            actor,
                            target,
                            reference_for=residence_components.repository.reference_for,
                            labels=RESIDENCE_LABELS,
                            prefix="f6",
                        )
                    except PermissionError:
                        if not target.creation_receipt:
                            raise
                        return project_own_receipt_target(
                            actor,
                            target,
                            reference_for=residence_components.repository.creation_receipt_reference_for,
                            labels=RESIDENCE_LABELS,
                            prefix="f6audit",
                        )
                if target.fact_kind in EDUCATION_LABELS:
                    # Catalog labels are not the owner's exact read authority.
                    # New F6 dependencies may read existing roots with no creation entry.
                    try:
                        return project_own_receipt_target(
                            actor,
                            target,
                            reference_for=residence_dependencies.repository.reference_for,
                            labels=EDUCATION_LABELS,
                            prefix="f4",
                        )
                    except PermissionError:
                        if retained_residence_projection is not None:
                            try:
                                return retained_residence_projection(actor, target)
                            except PermissionError:
                                pass
                        if not target.creation_receipt:
                            raise
                        return project_own_receipt_target(
                            actor,
                            target,
                            reference_for=residence_dependencies.repository.creation_receipt_reference_for,
                            labels=EDUCATION_LABELS,
                            prefix="f4audit",
                        )
                if (target.fact_kind, target.source_key) in residence_dependency_coordinates:
                    return residence_dependency_projection(actor, target)
                if retained_residence_projection is None:
                    raise ContactProductUnavailable("receipt inaccessible")
                return retained_residence_projection(actor, target)

            shared_receipt_projection = residence_receipt_projection

            def resolve_residence_change(**kwargs):
                adapter = (
                    quality_adapter
                    if kwargs["payload"].fact_kind == "fact_quality"
                    else residence_eligibility
                    if kwargs["payload"].fact_kind == "student_school_relationship"
                    else residence_adapter
                )
                resolved = adapter.resolve_change(**kwargs)
                return (
                    resolved
                    if adapter is residence_adapter
                    else residence_adapter.attach_time_basis(
                        actor=kwargs["actor"], resolved=resolved
                    )
                )

            def require_residence_change(actor, change_set):
                if any(c.fact_kind == "fact_quality" for c in change_set.changes):
                    return residence_quality.require_change_set(actor, change_set).revalidate()
                return require_partitioned_maintenance(
                    actor,
                    change_set,
                    residence=residence_components,
                    retained=residence_dependencies.require_maintenance,
                )

            residence_product = DirectoryFactProductService(
                preflight_rejection_formatter=format_residence_preflight_rejection,
                boundary=services.writer_boundary,
                resolve_change=resolve_residence_change,
                require_maintenance=feature_guard(require_residence_change),
                require_receipt=residence_components.require_receipt,
                plan_store=plan_store,
                receipt_reference_for=residence_components.repository.reference_for,
                receipt_fact_labels=RESIDENCE_LABELS,
                receipt_reference_prefix="f6",
                receipt_uses_current_targets=True,
                receipt_target_projection=project_shared_receipt,
            )
            residence_gateway = ResidenceToolGateway(
                resolve_actor=resolve_staff_actor,
                query=residence_components.query,
                product=residence_product,
                describe_adapter=residence_views,
                retention_seconds=retention_seconds,
                quality_adapter=quality_adapter,
                eligibility_adapter=residence_eligibility,
            )
            if directory_gateway is not None:
                directory_gateway.receipt_current_query = partial(
                    read_receipt_current_residence,
                    residence_gateway,
                    directory_gateway.receipt_current_query,
                )
            family_residence_gateway = FamilyResidenceToolGateway(
                resolve_actor=identity.resolve_private_actor,
                query=residence_components.family_query,
            )

        catalog = StaticAssistantCatalog(
            entries=(
                AssistantModuleCatalogEntry(
                    module_ref=DIRECTORY_MODULE
                    if directory_gateway is not None
                    else OFFICIAL_CONTACT_MODULE,
                    model_safe_capability=(
                        "学校基础事实的授权读取、完整制度原文及受控维护"
                        if directory_gateway is not None
                        else "正式学校官方联系查询与受控维护"
                    ),
                    module=directory_gateway if directory_gateway is not None else gateway,
                ),
                *(
                    (
                        AssistantModuleCatalogEntry(
                            module_ref=FAMILY_STAFF_MODULE,
                            model_safe_capability="学生、家校关系与可选地区的授权读取、核验和受控维护",
                            module=family_staff_gateway,
                        ),
                    )
                    if family_staff_gateway is not None
                    else ()
                ),
                *(
                    (
                        AssistantModuleCatalogEntry(
                            module_ref=EDUCATION_MODULE,
                            model_safe_capability="年级班级、班主任、固定教室与学生职务的授权查询和受控维护",
                            module=education_gateway,
                        ),
                    )
                    if education_gateway is not None
                    else ()
                ),
                *(
                    (
                        AssistantModuleCatalogEntry(
                            module_ref=RESIDENCE_MODULE,
                            model_safe_capability="获准寄宿楼房床位和正式住宿安排查询与受控维护",
                            module=residence_gateway,
                        ),
                    )
                    if residence_gateway is not None
                    else ()
                ),
            )
        )
        controls = AssistantControlService(
            catalog=catalog,
            store=RedisControlCredentialStore(redis=cast(ControlRedisClient, redis)),
            signing_keys={
                "f1": configuration.control_signing_key.get_secret_value().encode("utf-8")
            },
            active_key_id="f1",
        )
        staff_gateway_factory = (
            directory_gateway.for_turn if directory_gateway is not None else gateway.for_turn
        )

        if family_corrections is not None:
            from app.composition.school_fact_family_correction_gateway import (
                FamilyCorrectionToolGateway,
            )

            family_correction_gateway = FamilyCorrectionToolGateway(
                resolve_actor=identity.resolve_private_actor, product=family_corrections
            )

        family_children_gateway = (
            FamilyChildrenToolGateway(
                identity=identity,
                gateways=tuple(
                    item
                    for item in (
                        family_gateway,
                        family_correction_gateway,
                        family_teaching_gateway,
                        leadership_gateway,
                        family_residence_gateway,
                    )
                    if item is not None
                ),
                school_tool_names=frozenset({"school_facts.family.leadership"}),
            )
            if family_gateway is not None
            else None
        )

        def gateway_factory(actor_context_ref: str) -> ToolGateway:
            if family_children_gateway is not None:
                selected = identity.resolve_assistant_identity(actor_context_ref)
                if type(selected.actor) is ProductFamilyActor:
                    return family_children_gateway.for_turn(actor_context_ref)
            staff_turn = staff_gateway_factory(actor_context_ref)
            turns = [staff_turn]
            if family_staff_gateway is not None:
                turns.append(family_staff_gateway.for_turn(actor_context_ref))
            if education_gateway is not None:
                turns.append(education_gateway.for_turn(actor_context_ref))
            if teaching_gateway is not None:
                turns.append(teaching_gateway.for_turn(actor_context_ref))
            if residence_gateway is not None:
                turns.append(residence_gateway.for_turn(actor_context_ref))
            return (
                CombinedFactTurnGateway(
                    *turns,
                    unadvertised_tools=shared_own_receipt_tool_aliases(
                        directory_gateway, family_staff_gateway, education_gateway
                    ),
                )
                if len(turns) > 1
                else staff_turn
            )

        package_router = None
        package_stores = ()
        if configuration.purpose in _F7_PURPOSES:
            if (
                directory_components is None
                or family_staff is None
                or family_quality_existing is None
                or family_quality_changes is None
            ):
                raise ValueError("F7 packages require their complete owner authorization wiring.")
            package_sources = InMemoryPrivateSchoolFactSourceStore()
            package_plans = InMemorySchoolFactPlanStore()
            package_stores = (package_sources, package_plans)
            package_guards = {
                "directory": f2_guard.require_change_set,
                "family": family_staff.require_maintenance,
                "family_quality": lambda actor, batch: family_quality_changes.require_change_set(
                    actor, batch
                ).revalidate(),
            }
            if education_components is not None:
                package_guards.update(
                    {
                        "education": education_components.require_maintenance,
                        "education_quality": lambda actor, batch: (
                            education_components.quality.require_change_set(
                                actor, batch
                            ).revalidate()
                        ),
                    }
                )
            if residence_components is not None:
                package_guards.update(
                    {
                        "residence": residence_components.require_maintenance,
                        "residence_quality": lambda actor, batch: (
                            residence_quality.require_change_set(actor, batch).revalidate()
                        ),
                    }
                )
            package_partition = PackageFactPartition(
                directory_configuration=configuration.directory,
                family_configuration=configuration.family,
                education_configuration=configuration.education,
                existing_quality_kind=existing_quality_kind,
                live_directory_guard=f2_guard,
            )
            family_quality_current = FamilyQualityCurrentContent(
                f2_guard=f2_guard,
                authorizer=family_quality_authorizer,
                projection=family_quality_existing.projection,
                reader=family_staff.query.quality_reader,
                clock=lambda: datetime.now(UTC),
            )
            package_authority = PackageChangeAuthorization(
                package_partition,
                package_guards,
                references=PackageReferenceAuthorization(
                    package_partition,
                    directory_guard=f2_guard,
                    family_authority=family_staff.authority,
                    education_authority=(
                        education_components.authority if education_components is not None else None
                    ),
                    residence_authority=(
                        residence_components.authority if residence_components is not None else None
                    ),
                    quality_reads=package_quality_read_proofs(
                        education=education_components.quality if education_components else None,
                        residence=residence_quality if residence_components else None,
                        family=lambda actor, _kind, key, _at: family_quality_current(actor, key)[
                            "proof"
                        ],
                    ),
                ),
            )

            def package_education_quality_snapshot(actor, kind, key, at):
                value = education_components.quality.read_for_source(actor, kind, key, at)
                return {
                    **value,
                    "version": value["row"]["version"],
                    "valid_from": value["row"].get("effective_at"),
                    "valid_to": value["row"].get("valid_to"),
                    "school_fact_version": value["proof"].school_fact_version,
                }

            package_replacement = PackageReplacementReader(
                partition=package_partition,
                references=package_authority.references,
                directory=directory_components,
                family=family_staff,
                education=education_components,
                residence=residence_components,
                directory_adapter=directory_adapter,
                family_adapter=family_staff.adapter,
                residence_adapter=residence_adapter if residence_components is not None else None,
                family_resolver=resolve_family_change,
                education_resolver=resolve_education_change,
                school_relation_resolver=(
                    residence_eligibility.resolve_change
                    if residence_components is not None
                    else None
                ),
                quality_snapshots={
                    "family_quality": lambda actor, _kind, key, _at: family_quality_current(
                        actor, key
                    ),
                    **(
                        {"education_quality": package_education_quality_snapshot}
                        if education_components is not None
                        else {}
                    ),
                    **(
                        {
                            "residence_quality": lambda actor, _kind, key, _at: (
                                residence_quality.read_content(actor, key, action="read")
                            )
                        }
                        if residence_components is not None
                        else {}
                    ),
                },
                quality_adapters={
                    "family_quality": family_quality_adapter,
                    **(
                        {"education_quality": education_components.dependencies}
                        if education_components is not None
                        else {}
                    ),
                    **(
                        {"residence_quality": quality_adapter}
                        if residence_components is not None
                        else {}
                    ),
                },
            )

            async def package_identity(request: Request):
                resolved = staff_http_identity(request)
                with bind_directory_identity(resolved.actor_context_ref):
                    # Source upload permission is not fact maintenance authority.
                    yield SchoolFactRequestIdentity(
                        actor=resolved.actor,
                        session_binding=resolved.session_binding,
                        authorized_actions=frozenset({"school_facts.package.submit"}),
                    )

            def require_package_changes(selected, change_set):
                require_product_fact_features(configuration, change_set)
                package_authority(selected.actor, change_set)

            def require_package_result(selected):
                from uuid import uuid4

                from app.platform.identity.execution_context import compose_command_context

                proof = directory_components.own_terminal_authorizer(
                    compose_command_context(selected.actor, uuid4(), "package-own-result")
                )
                return proof.revalidate

            from app.composition.school_fact_directory_product import FactChangeInput
            from app.composition.school_fact_education_product import EducationBatchInput
            from app.composition.school_fact_residence_product import ResidenceBatchInput

            package_form_resolvers = {
                "directory": (FactChangeInput, resolve_directory_change),
                "family": (FactChangeInput, resolve_family_change),
            }
            if education_components is not None:
                package_form_resolvers["education"] = (
                    EducationBatchInput,
                    resolve_education_change,
                )
            if residence_components is not None:
                package_form_resolvers["residence"] = (
                    ResidenceBatchInput,
                    resolve_residence_change,
                )

            package_router = create_school_fact_router(
                change_service=services.writer_boundary,
                source_store=package_sources,
                plan_store=package_plans,
                identity_dependency=package_identity,
                enable_package=True,
                package_only=True,
                result_only=not configuration.f7_packages_enabled,
                authorize_package=require_package_changes,
                authorize_package_result=require_package_result,
                package_replacement=package_replacement,
                package_source_adapter=PackageDomainSourceAdapter(
                    form_resolvers=package_form_resolvers,
                    education_resolver=resolve_education_change if education_components else None,
                    residence_resolver=residence_adapter.resolve_change
                    if residence_components
                    else None,
                ),
                authorize_package_selection=lambda selected, targets: (
                    package_authority.references.require_selected(selected.actor, targets)
                ),
            )

        staff_domain_instruction = (
            DIRECTORY_ASSISTANT_INSTRUCTION if directory_gateway is not None else ""
        )
        # Domain contracts explain evidence; the common user-facing contract applies
        # to the completed answer after all installed domains have been described.
        answer_instruction = LIVE_ASSISTANT_INSTRUCTION if is_live else F1_ASSISTANT_INSTRUCTION
        instruction: OwnerInstruction = staff_domain_instruction + answer_instruction
        if family_gateway is not None:
            from app.composition.school_fact_family_staff_gateway import (
                family_staff_instruction,
            )

            def family_owner_instruction(actor_context_ref: str) -> str:
                selected = identity.resolve_assistant_identity(actor_context_ref)
                if type(selected.actor) is ProductFamilyActor:
                    return (
                        FAMILY_CHILDREN_INSTRUCTION
                        + FAMILY_ASSISTANT_INSTRUCTION
                        + (
                            FAMILY_TEACHING_INSTRUCTION
                            if family_teaching_gateway is not None
                            else ""
                        )
                        + (
                            FAMILY_RESIDENCE_INSTRUCTION
                            if family_residence_gateway is not None
                            else ""
                        )
                        + answer_instruction
                    )
                if type(selected.actor) is StaffActorContext:
                    return (
                        staff_domain_instruction
                        + family_staff_instruction(education_enabled=education_gateway is not None)
                        + (EDUCATION_ASSISTANT_INSTRUCTION if education_gateway is not None else "")
                        + (TEACHING_ASSISTANT_INSTRUCTION if teaching_gateway is not None else "")
                        + (RESIDENCE_INSTRUCTION if residence_gateway is not None else "")
                        + answer_instruction
                    )
                raise AssistantRuntimeError("current assistant owner unavailable")

            instruction = family_owner_instruction
        turn_instruction: TurnInstruction | None = None
        user_input_guard: Callable[[str], None] | None = None
        if isinstance(configuration, LiveSchoolFactProductConfiguration) and not assistant_disabled:
            from app.composition.school_fact_live_model_policy import (
                check_live_user_text,
                wrap_live_gateway,
            )

            original_gateway_factory = gateway_factory
            data_policy = configuration.model_data_policy
            if configuration.model is None or data_policy is None:
                raise ProductConfigurationError("Formal assistant model approvals are absent.")
            user_input_guard = partial(
                check_live_user_text,
                known_credentials=(
                    load_product_model_api_key(configuration.model).get_secret_value(),
                    settings.postgres_password,
                    settings.redis_password,
                    settings.rls_context_signing_key.get_secret_value(),
                    settings.dingtalk_app_secret or "",
                    configuration.control_signing_key.get_secret_value(),
                    configuration.reader.database_password.get_secret_value(),
                    configuration.reader.cursor_hmac_key.get_secret_value(),
                    configuration.executor.caller_auth_key.get_secret_value(),
                    configuration.executor.context_transport_hmac_key.get_secret_value(),
                ),
            )

            def gateway_factory(actor_context_ref: str) -> ToolGateway:
                return wrap_live_gateway(
                    original_gateway_factory(actor_context_ref), data_policy
                )

        uses_formal_builder = False
        if (
            assistant_loop_factory is None
            and configuration.purpose
            in {
                "school-facts-f2-synthetic",
                "school-facts-f3-synthetic",
                "school-facts-f4-synthetic",
                "school-facts-f5-synthetic",
                "school-facts-f6-synthetic",
                "school-facts-f7-synthetic",
                "school-facts-live",
                "school-facts-live-integration-synthetic",
            }
            and configuration.directory is not None
            and configuration.model is not None
        ):
            uses_formal_builder = True
        if assistant_disabled:
            loop = None
        elif uses_formal_builder:
            from app.composition.school_fact_pydanticai import build_pydanticai_loop

            loop = build_pydanticai_loop(
                configuration=configuration,
                gateway_factory=gateway_factory,
                control_service=controls,
                instruction=instruction,
                turn_instruction=turn_instruction,
            )
        elif assistant_loop_factory is None:
            if not isinstance(configuration, SchoolFactProductConfiguration):
                raise ProductConfigurationError(
                    "Live school facts require the formal assistant implementation."
                )
            loop = AssistantToolLoop(
                model=build_product_model(configuration.model),
                gateway_factory=gateway_factory,
                control_service=controls,
                instruction=instruction,
                turn_instruction=turn_instruction,
            )
        else:
            loop = assistant_loop_factory(
                configuration=configuration,
                gateway_factory=gateway_factory,
                control_service=controls,
                instruction=instruction,
            )

        def assistant_identity(request: Request) -> AssistantRequestIdentity:
            resolved = identity_http.assistant_request_dependency(request)
            return AssistantRequestIdentity(actor_context_ref=resolved.actor_context_ref)

        # Local in-memory engineering bounds; no durable prompt or provider retention claim.
        assistant_runtime = AssistantRuntime(
            retention_seconds=retention_seconds,
            max_conversations=128,
        )
        janitor = SchoolFactEphemeralStoreJanitor(
            assistant_runtime,
            contacts,
            plan_store,
            gateway,
            *package_stores,
            *(
                (directory_product, directory_gateway)
                if directory_product is not None and directory_gateway is not None
                else ()
            ),
            *((family_product,) if family_product is not None else ()),
            *((family_staff_gateway,) if family_staff_gateway is not None else ()),
            *((education_product,) if education_product is not None else ()),
            *((education_gateway,) if education_gateway is not None else ()),
            *((residence_product,) if residence_product is not None else ()),
            *((residence_gateway,) if residence_gateway is not None else ()),
            interval_seconds=1.0,
        )

        @asynccontextmanager
        async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
            try:
                janitor.start()
                yield
            finally:
                try:
                    janitor.stop()
                finally:
                    resources.close()

        app = FastAPI(
            title="学校事实与小静" if is_live else "学校事实基础与小静 · 合成学校工程环境",
            version="live-v1" if is_live else "f7"
            if configuration.purpose in _F7_PURPOSES
            else "f6"
            if configuration.purpose == "school-facts-f6-synthetic"
            else "f5"
            if configuration.purpose in _F5_PLUS_PURPOSES
            else "f4"
            if configuration.education is not None and configuration.education.enabled
            else "f3"
            if configuration.family is not None
            else "f2"
            if configuration.directory is not None
            else "f1",
            lifespan=lifespan,
            responses={422: {"model": HttpErrorResponse}},
        )
        app.middleware("http")(assign_request_id)
        app.add_exception_handler(ApiError, api_error_handler)
        app.add_exception_handler(RequestValidationError, request_validation_error_handler)

        @app.get("/api/v1/school-fact-product/features")
        def product_features():
            return {
                **({"assistant_enabled": not assistant_disabled} if is_live else {}),
                "f5_features_enabled": configuration.purpose in _F5_PLUS_PURPOSES
                and configuration.f5_features_enabled,
                "f6_features_enabled": residence_components is not None,
                "f7_packages_enabled": package_router is not None
                and configuration.f7_packages_enabled,
            }

        if is_live:
            install_dingtalk_identity_dependencies(
                app, settings=settings, sessions=sessions, redis=redis,
            )
        app.include_router(identity_http.router)
        if package_router is not None:
            app.include_router(package_router)
        if residence_components is not None:
            app.include_router(
                create_family_residence_router(
                    identity_dependency=identity_http.request_dependency,
                    query=residence_components.family_query,
                    resolve_identity=identity.resolve_private_actor,
                )
            )
        if family_teaching is not None:
            app.include_router(
                create_teaching_read_router(
                    identity_dependency=identity_http.request_dependency,
                    resolve_identity=identity.resolve_private_actor,
                    family_teaching=family_teaching,
                    leadership=family_leadership,
                    staff_teaching=teaching_query,
                )
            )
        if family_query is not None:
            app.include_router(
                create_family_profile_router(
                    identity_dependency=identity_http.request_dependency,
                    query=family_query,
                    education=family_education,
                    resolve_identity=identity.resolve_private_actor,
                )
            )
        if family_corrections is not None:
            app.include_router(
                create_family_correction_router(
                    identity_dependency=identity_http.request_dependency,
                    product=family_corrections,
                )
            )
        if directory_product is None:
            app.include_router(
                create_school_fact_product_contact_router(
                    identity_dependency=staff_http_identity,
                    query=services.query_repository,
                    contacts=contacts,
                )
            )
        else:
            app.include_router(
                create_school_fact_product_receipt_router(
                    identity_dependency=staff_http_identity,
                    contacts=directory_product,
                )
            )
        if directory_product is not None and directory_adapter is not None:

            async def directory_identity(request: Request) -> AsyncGenerator[StaffProductIdentity]:
                resolved = staff_http_identity(request)
                with bind_directory_identity(resolved.actor_context_ref):
                    yield resolved

            if family_staff is not None and family_product is not None:
                app.include_router(
                    create_family_staff_product_router(
                        identity_dependency=directory_identity,
                        query=family_staff.query,
                        product=family_product,
                    )
                )
            if education_components is not None and education_product is not None:
                app.include_router(
                    create_education_product_router(
                        identity_dependency=directory_identity,
                        query=education_components.views,
                        education_query=education_components.query,
                        product=education_product,
                        teaching_enabled=configuration.purpose in _F5_PLUS_PURPOSES
                        and configuration.f5_features_enabled,
                    )
                )
            if residence_views is not None and residence_product is not None:
                app.include_router(
                    create_residence_product_router(
                        identity_dependency=directory_identity,
                        views=residence_views,
                        product=residence_product,
                    )
                )
            app.include_router(
                create_directory_product_router(
                    identity_dependency=directory_identity,
                    query=directory_adapter,
                    product=directory_product,
                )
            )
        if assistant_disabled:
            def assistant_not_enabled(request: Request) -> None:
                assistant_identity(request)
                raise ApiError(
                    status_code=503,
                    code="assistant.not_enabled",
                    message="小静尚未启用，可继续在页面查看获准资料并办理有权限的维护。",
                )

            for method in ("GET", "POST"):
                app.add_api_route(
                    "/api/v1/assistant/{assistant_path:path}",
                    assistant_not_enabled,
                    methods=[method],
                    name="assistant_not_enabled_" + method.lower(),
                    status_code=503,
                    response_model=HttpErrorResponse,
                )
        else:
            assert loop is not None
            app.include_router(
                create_assistant_router(
                    runtime=assistant_runtime,
                    identity_dependency=assistant_identity,
                    turn_orchestrator=loop,
                    control_service=controls,
                    on_user_input=user_input_guard,
                )
            )
        return app
    except BaseException:
        resources.close()
        raise
