"""F2 fact-domain maintenance adapter; all writes remain behind SchoolFactChangeBoundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from re import fullmatch
from threading import RLock
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Literal, Protocol, Self, cast, get_args
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)
from pydantic_core import ErrorType, PydanticCustomError

from app.composition.school_fact_product_contacts import ContactProductUnavailable, ContactReceipt
from app.composition.school_facts import InMemorySchoolFactPlanStore
from app.domains.students.education_contracts import EDUCATION_FACT_KINDS
from app.platform.assistant.contracts import JsonObject
from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.authorization.f2_reader_compiler import F2_DEFINITIONS, query_selector_support
from app.platform.contracts import FrozenContractModel, StaffActorContext
from app.platform.identity.execution_context import compose_command_context
from app.platform.master_data_imports.ports import PrivateSourceOwner
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.change_boundary import (
    SchoolFactChangeBoundary,
    SchoolFactOwnResultBoundary,
)
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from app.platform.school_facts.f6_policy import F6_FACT_KINDS
from app.platform.school_facts.ports import (
    SchoolFactChangeProvenance,
    SchoolFactOwnResult,
    SchoolFactOwnResultTarget,
    SchoolFactOwnTargetPage,
)


def directory_record_field_states(record: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Describe absent scalar values only on authorized, fixed F2 read records.

    The source/reference shape identifies this contract, never grants authority.
    F2's fixed Reader selects every Definition.fields column, then strips SQL nulls.
    Missing means this record did not provide a value, not a real-world negative.
    Relations, derived fields and maintenance-only fields are not inferred here.
    """
    kind = record.get("fact_kind")
    reference = record.get("reference")
    source = record.get("source")
    fields = record.get("fields")
    if not (
        isinstance(kind, str)
        and kind in F2_DEFINITIONS
        and isinstance(reference, str)
        and reference.startswith(f"f2:{kind}:")
        and len(reference) > len(f"f2:{kind}:")
        and isinstance(source, dict)
        and source.get("kind") == "formal_publication"
        and isinstance(fields, dict)
    ):
        return {}
    return {
        field: "not_provided"
        for field in F2_DEFINITIONS[kind].fields
        if not field.endswith("_source_key")
        and fields.get(field) is None
        # The gateway's explicit policy excerpt represents a provided original.
        and not (field == "content_text" and isinstance(fields.get("original_text"), dict))
    }


def directory_record_query_names(record: Mapping[str, JsonValue]) -> dict[str, JsonValue] | None:
    """Offer exact selector values from this authorized record's fixed read contract."""
    kind = record.get("fact_kind")
    reference = record.get("reference")
    source = record.get("source")
    fields = record.get("fields")
    if not (
        isinstance(kind, str)
        and kind in F2_DEFINITIONS
        and isinstance(reference, str)
        and reference.startswith(f"f2:{kind}:")
        and len(reference) > len(f"f2:{kind}:")
        and isinstance(source, dict)
        and source.get("kind") == "formal_publication"
        and isinstance(fields, dict)
    ):
        return None
    return {
        name: value
        for name in cast(list[str], query_selector_support(kind)["name_fields"])
        if isinstance(value := fields.get(name), str) and value
    }


type F2FactKind = Literal[
    "school",
    "person",
    "organization",
    "organization_campus_relationship",
    "assignment",
    "location",
    "location_alias",
    "person_contact",
    "official_contact",
    "policy",
    "policy_scope",
    "policy_version",
    "organization_profile",
    "organization_membership",
    "organization_location",
    "person_workplace",
    "school_service",
    "fact_quality",
]
type F3MaintenanceFactKind = Literal[
    "person",
    "login_mapping",
    "assignment",
    "student",
    "student_school_relationship",
    "student_contact",
    "guardian_relationship",
    "person_region",
]
type F4MaintenanceFactKind = Literal[
    "education_stage",
    "education_offering",
    "education_grade",
    "education_calendar_rule",
    "education_term",
    "education_grade_group",
    "education_cohort",
    "student_cohort_membership",
    "education_class",
    "class_placement",
    "class_lineage",
    "student_grade_placement",
    "student_class_membership",
    "class_teacher_assignment",
    "class_location",
    "class_position_definition",
    "student_class_position",
]
type F5MaintenanceFactKind = Literal[
    "teaching_subject",
    "teaching_course",
    "course_subject",
    "teaching_offering",
    "offering_member_source",
    "teaching_group",
    "teaching_group_member",
    "teaching_assignment",
    "teaching_organization_scope",
]
type F6MaintenanceFactKind = Literal[
    "dormitory_building_profile", "dorm_room", "dorm_bed", "student_bed_residence"
]
type MaintenanceFactKind = (
    F2FactKind
    | F3MaintenanceFactKind
    | F4MaintenanceFactKind
    | F5MaintenanceFactKind
    | F6MaintenanceFactKind
)
_MAINTENANCE_FACT_KINDS = (
    {
        "school",
        "person",
        "organization",
        "organization_campus_relationship",
        "assignment",
        "location",
        "location_alias",
        "person_contact",
        "official_contact",
        "policy",
        "policy_scope",
        "policy_version",
        "organization_profile",
        "organization_membership",
        "organization_location",
        "person_workplace",
        "school_service",
        "fact_quality",
        "login_mapping",
        "student",
        "student_school_relationship",
        "student_contact",
        "guardian_relationship",
        "person_region",
    }
    | EDUCATION_FACT_KINDS
    | F6_FACT_KINDS
)
Text = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1000)
]
Reference = Annotated[
    str,
    StringConstraints(strict=True, min_length=8, max_length=1600, pattern=r"^[A-Za-z0-9_.:-]+$"),
]


class FactReferenceChoicesInput(FrozenContractModel):
    fact_kind: str = Field(min_length=1, max_length=80)
    field_name: str = Field(min_length=1, max_length=80)


def reference_choice_kind(descriptor: dict, field_name: str) -> str:
    fields = [f for f in descriptor.get("fields", ()) if f.get("name") == field_name]
    if len(fields) != 1 or fields[0].get("type") not in {"reference", "references"}:
        raise ValueError("请选择该类资料已声明的关联字段。")
    kind = fields[0].get("reference_kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("该关联需要先明确对应对象类型。")
    return kind


class FactQueryInput(FrozenContractModel):
    fact_kind: F2FactKind = Field(
        description=(
            "关系语义。organization_campus_relationship维护组织与校区的正式关联；"
            "organization_location仅表示组织办公或接待地点，不等于组织正式校区关联；"
            "person_workplace表示人员或任职办公地点，不证明实时在场；"
            "organization_profile保存部门职能，通过organization引用查询。"
            "location_alias只解析正式别名，不负责组织归属；地点父子关系也不是组织归属。"
            "只选择能回答当前目标的关系证据，不以遍历办公地点或别名代替正式组织校区关系。"
            "所有事实族均支持本族reference和获准列表；名称只精确匹配，关联只匹配直接关系。各族可用名称字段name_fields和关联对象类型related_kinds（来自固定查询定义）："
            + json.dumps(
                {
                    kind: {
                        key: value
                        for key, value in query_selector_support(kind).items()
                        if key in {"name_fields", "related_kinds"}
                    }
                    for kind in F2_DEFINITIONS
                },
                ensure_ascii=False,
            )
        )
    )
    query_text: Text | None = Field(
        default=None,
        description=(
            "用户给出具体目标名称且该族支持name_fields时优先直接传入，仅精确匹配；不是模糊搜索、简称搜索或关联对象名称搜索。"
            "name_fields为空时不要传；先在关联对象所属事实族取得引用，再用支持的related_ref。"
        ),
    )
    reference: Reference | None = Field(
        default=None,
        description="该记录自身的已返回引用，必须属于fact_kind；关联对象引用应放related_ref，不能放reference。",
    )
    related_ref: Reference | None = Field(
        default=None,
        description=(
            "仅该族related_kinds列明类型的已返回引用，匹配直接关系；不隐含下级、校区内所有地点或其他关系。"
        ),
    )
    include_supporting_evidence: bool = Field(
        default=False,
        description=(
            "每条记录已随附source发布来源，普通来源追问直接使用，不需要重新查询或describe。"
            "仅用户要求逐字核对原始依据字段、文号或维护依据时开启；原值未分类为文件名、文号或追踪值，不据格式猜用途。"
            "此开关不返回路径或业务编码，完整依据也保留在页面展开证据中。"
        ),
    )
    include_routing_details: bool = Field(
        default=False,
        description="仅用户明确核对原记录路径或业务编码时开启；不用于普通来源说明，也不返回原始依据值。路径不作正式名称或关联证据。",
    )
    cursor: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = Field(
        default=None,
        description="仅使用本次目录结果next_cursor续读，保持原查询条件与page_size；失效时重新查询。游标不增加权限。",
    )
    page_size: int = Field(
        default=20,
        ge=1,
        le=100,
        strict=True,
        description="每批最多返回的获准条目数；材料较大时可能少于此数，按next_cursor继续。",
    )
    as_of: AwareDatetime | None = None
    scope: Literal["selected", "all_authorized"] = Field(
        default="selected",
        description=(
            "具体目标使用selected并传query_text、reference或支持的related_ref；"
            "all_authorized仅用于当前问题确需列表或探索获准集合，不作为具体目标查询的前置步骤。"
        ),
    )

    @model_validator(mode="after")
    def selection(self) -> Self:
        if self.reference is not None and (
            self.query_text is not None or self.related_ref is not None
        ):
            raise ValueError("只能使用名称条件或已返回的对象引用之一。")
        if self.scope == "all_authorized" and (
            self.query_text is not None
            or self.reference is not None
            or self.related_ref is not None
        ):
            raise ValueError("获准集合查询不能附带单一对象选择。")
        return self


class FactChangeInput(FrozenContractModel):
    safe_constraint_messages: ClassVar[Mapping[str, str]] = MappingProxyType(
        {
            "fact_education_operation": "correct和cancel仅用于已支持的教育纠正或未来撤销。",
            "fact_target_choice": (
                "target_ref和creation_ref必须且只能填写一个；修改已有对象用target_ref，创建用creation_ref。"
            ),
            "fact_creation_operation": "使用creation_ref创建对象时，operation只能是upsert。",
            "fact_reserved_fields": (
                "fields只接受业务字段，不能携带school_id、source_key、expected_version、actor或scope_id。"
            ),
        }
    )
    fact_kind: MaintenanceFactKind
    target_ref: Reference | None = Field(
        default=None,
        description="已返回的现有对象引用；与creation_ref必须且只能填写一个。",
    )
    creation_ref: Reference | None = Field(
        default=None,
        description="已获准创建项引用；与target_ref必须且只能填写一个，且operation只能是upsert。",
    )
    operation: Literal["upsert", "end", "revoke", "reactivate", "correct", "cancel"] = "upsert"
    fields: dict[str, JsonValue] = Field(max_length=70)
    effective_at: AwareDatetime | None = None
    reason: Text

    @classmethod
    def _constraint_error(cls, code: str) -> PydanticCustomError:
        return PydanticCustomError(code, cls.safe_constraint_messages[code])

    @model_validator(mode="after")
    def target(self) -> Self:
        if (
            self.operation in {"correct", "cancel"}
            and self.fact_kind not in EDUCATION_FACT_KINDS | F6_FACT_KINDS
        ):
            raise self._constraint_error("fact_education_operation")
        if (self.target_ref is None) == (self.creation_ref is None):
            raise self._constraint_error("fact_target_choice")
        if self.creation_ref is not None and self.operation != "upsert":
            raise self._constraint_error("fact_creation_operation")
        if any(
            k in self.fields
            for k in ("school_id", "source_key", "expected_version", "actor", "scope_id")
        ):
            raise self._constraint_error("fact_reserved_fields")
        return self


def prepare_argument_errors(
    error: ValidationError, input_model: type[FactChangeInput]
) -> list[JsonObject]:
    """Release schema coordinates and exact owner-defined constraints, never exception data."""
    schema = input_model.model_json_schema()
    paths: set[tuple[str | None, ...]] = set()

    def collect(node: dict[str, Any], prefix: tuple[str | None, ...]) -> None:
        if len(prefix) >= 6:
            return
        if "$ref" in node:
            node = schema.get("$defs", {}).get(node["$ref"].removeprefix("#/$defs/"), {})
        for name, child in node.get("properties", {}).items():
            path = (*prefix, name)
            paths.add(path)
            collect(child, path)
        if isinstance(node.get("items"), dict):
            path = (*prefix, None)
            paths.add(path)
            collect(node["items"], path)
        for child in node.get("anyOf", ()):
            collect(child, prefix)

    collect(schema, ())
    allowed_types = frozenset(get_args(ErrorType))
    results: list[JsonObject] = []
    for item in error.errors(include_input=False, include_context=True, include_url=False)[:8]:
        path: tuple[str | None, ...] = ()
        location: list[JsonValue] = []
        for part in item["loc"][:6]:
            token = part if isinstance(part, str) else None
            path = (*path, token)
            if path not in paths or (not isinstance(part, str) and not 0 <= part < 300):
                location.append("*")
                break
            location.append(part)
        projected: JsonObject = {
            "type": item["type"] if item["type"] in allowed_types else "value_error",
            "loc": location,
        }
        reason = input_model.safe_constraint_messages.get(item["type"])
        # Exact static message and no context identify our fixed constraint.
        # A custom type with an arbitrary message/context cannot claim this reason.
        if reason is not None and item["msg"] == reason and not item.get("ctx"):
            projected.update(code=item["type"], reason=reason)
        results.append(projected)
    return results


class FactChangeSummary(FrozenContractModel):
    fact_timezone_name: str | None = None
    time_basis: dict[str, JsonValue] | None = None
    reference_labels: dict[str, str] = Field(default_factory=dict)
    reference_labels_as_of: AwareDatetime | None = None
    fact_kind: MaintenanceFactKind
    label: str
    reference: str
    operation: str
    before: dict[str, JsonValue] | None
    after: dict[str, JsonValue] | None
    effective_at: AwareDatetime
    reason: str
    source_label: str
    impacts: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=300)
    before_segments: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=1000)
    after_segments: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=1000)
    regroup: dict[str, JsonValue] | None = None


class FactProposal(FrozenContractModel):
    proposal_ref: str
    summary: FactChangeSummary
    expires_at: AwareDatetime


class OwnReceiptTarget(FrozenContractModel):
    fact_kind: MaintenanceFactKind
    label: str
    reference: Reference
    operation: Literal["upsert", "end", "revoke", "reactivate", "correct", "cancel"]
    target_version: int = Field(ge=1)


class OwnReceiptTargetPage(FrozenContractModel):
    offset: int = Field(strict=True, ge=0)
    next_offset: int = Field(strict=True, ge=0)
    target_count: int = Field(strict=True, ge=1, le=50_000)
    has_more: bool

    @model_validator(mode="after")
    def valid_continuation(self) -> Self:
        if (
            not self.offset <= self.next_offset <= self.target_count
            or self.has_more != (self.next_offset < self.target_count)
        ):
            raise ValueError("invalid receipt continuation")
        return self


class OwnReceipt(ContactReceipt):
    kind: Literal["own_fact_receipt"] = "own_fact_receipt"
    confirmed_at: AwareDatetime
    targets: tuple[OwnReceiptTarget, ...]
    target_page: OwnReceiptTargetPage | None = Field(default=None, exclude_if=lambda v: v is None)
    read_only: Literal[True] = True

    @model_validator(mode="after")
    def require_full_or_paged_targets(self) -> Self:
        if self.target_page is None and not self.targets:
            raise ValueError("own receipt requires a target or a bounded page")
        return self


class OwnReceiptPage(FrozenContractModel):
    kind: Literal["own_fact_receipts"] = "own_fact_receipts"
    items: tuple[OwnReceipt, ...] = Field(max_length=20)
    has_more: bool
    notice: str = (
        "仅列出本人在当前授权范围内可读取的有限历史回执，不代表全部；"
        "仅记录原操作结果，当前可见性和时间有效性需另行查询核验。"
    )


class ReceiptReferenceFor(Protocol):
    def __call__(self, *, actor: StaffActorContext, fact_kind: str, source_key: str) -> str: ...


type ReceiptTargetProjection = Callable[
    [StaffActorContext, SchoolFactOwnResultTarget], OwnReceiptTarget
]


@dataclass(frozen=True)
class _OwnReceiptCheck:
    request_id: UUID
    expected: Mapping[str, JsonValue]
    limit: int | None
    receipt_ref: str | None
    target_offset: int
    target_limit: int


class _OwnReceiptValidation:
    """One fresh authorized page serves overlapping checks at a release boundary."""

    def __init__(
        self, product: DirectoryFactProductService, actor: StaffActorContext, session_binding: str
    ) -> None:
        self.product, self.actor, self.session_binding = product, actor, session_binding
        self.checks: list[_OwnReceiptCheck] = []
        self.identities: dict[tuple[int, str], Callable[[], None]] = {}

    def finish(self) -> None:
        for require_identity in self.identities.values():
            require_identity()
        limits = [check.limit for check in self.checks if check.limit is not None]
        request_id = self.checks[0].request_id
        page = None
        details: dict[tuple[str, int, int], dict[str, JsonValue]] = {}
        if limits:
            page = self.product.own_receipts(
                actor=self.actor,
                session_binding=self.session_binding,
                request_id=request_id,
                limit=max(limits),
            ).model_dump(mode="json")
            for item in cast(list[dict[str, JsonValue]], page["items"]):
                details[(cast(str, item["reference"]), 0, 20)] = item
        for check in self.checks:
            if check.limit is not None:
                if page is None:
                    raise AssistantRuntimeError("Own receipt page unavailable")
                view = deepcopy(page)
                items = cast(list[dict[str, JsonValue]], view["items"])
                view["has_more"] = bool(view["has_more"]) or len(items) > check.limit
                view["items"] = items[: check.limit]
            else:
                reference = check.receipt_ref
                if reference is None:
                    raise AssistantRuntimeError("Own receipt selection unavailable")
                key = (reference, check.target_offset, check.target_limit)
                if key not in details:
                    details[key] = self.product.own_receipt(
                        actor=self.actor,
                        session_binding=self.session_binding,
                        request_id=request_id,
                        receipt_ref=reference,
                        target_offset=check.target_offset,
                        target_limit=check.target_limit,
                    ).model_dump(mode="json")
                view = deepcopy(details[key])
            # Request IDs are presentation provenance from each original call,
            # not fields of the immutable receipt. Preserve each caller's ID.
            rows = view.get("items", [view])
            for row in cast(list[dict[str, JsonValue]], rows):
                if row.get("request_id") == str(request_id):
                    row["request_id"] = str(check.request_id)
            if view != check.expected:
                raise AssistantRuntimeError("Authorized own receipt evidence changed")
        for require_identity in self.identities.values():
            require_identity()


def validate_own_receipt_read(
    product: DirectoryFactProductService,
    *,
    actor: StaffActorContext,
    session_binding: str,
    request_id: UUID,
    expected: Mapping[str, JsonValue],
    require_identity: Callable[[], None],
    identity_binding: tuple[int, str],
    limit: int | None = None,
    receipt_ref: str | None = None,
    target_offset: int = 0,
    target_limit: int = 20,
) -> None:
    from app.composition.school_fact_read_evidence import validation_batch

    if (limit is None) == (receipt_ref is None):
        raise AssistantRuntimeError("Invalid own receipt validation selection")
    # Nested callers defer only within the existing synchronous release scope.
    # Without an outer scope this context finishes before returning to its caller.
    with validation_batch() as batch:
        key = (id(product), actor, session_binding)
        if key not in batch.own_receipts:
            batch.own_receipts[key] = _OwnReceiptValidation(product, actor, session_binding)
        # The gateway has just resolved this actor. Recheck that same resolver/ref
        # once before and after the shared read; distinct bindings remain distinct.
        batch.own_receipts[key].identities.setdefault(identity_binding, require_identity)
        batch.own_receipts[key].checks.append(
            _OwnReceiptCheck(
                request_id, deepcopy(expected), limit, receipt_ref, target_offset, target_limit
            )
        )


def project_own_receipt_target(
    actor: StaffActorContext,
    target: SchoolFactOwnResultTarget,
    *,
    reference_for: ReceiptReferenceFor,
    labels: Mapping[str, str],
    prefix: Literal["f2", "f3", "f4", "f4audit", "f6", "f6audit"],
) -> OwnReceiptTarget:
    if target.fact_kind not in labels or target.fact_kind not in _MAINTENANCE_FACT_KINDS:
        raise ContactProductUnavailable("receipt inaccessible")
    reference = reference_for(actor=actor, fact_kind=target.fact_kind, source_key=target.source_key)
    from app.platform.school_facts.f5_policy import F5_FACT_KINDS
    from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates

    # reference_for is the owner-authenticated resolver. This only validates its
    # output shape; a signed coordinate never replaces the current receipt guard.
    audit = prefix.endswith("audit")
    family = prefix.removesuffix("audit")
    if family == "f4" and target.fact_kind in F5_FACT_KINDS:
        family = "f5"
    school_reference = reference.removeprefix(prefix + ":") if audit else reference
    school_matches = (
        (not audit or reference.startswith(prefix + ":"))
        and school_scope_reference_coordinates(school_reference)
        == (family, target.fact_kind, audit)
    )
    if not reference.startswith(f"{prefix}:{target.fact_kind}:") and not school_matches:
        raise ContactProductUnavailable("receipt inaccessible")
    return OwnReceiptTarget(
        fact_kind=cast(MaintenanceFactKind, target.fact_kind),
        label=labels[target.fact_kind],
        reference=reference,
        operation=cast(
            Literal["upsert", "end", "revoke", "reactivate", "correct", "cancel"], target.operation
        ),
        target_version=target.target_version,
    )


@dataclass(frozen=True, slots=True)
class ResolvedFactChange:
    change_set: CanonicalFactChangeSetV1
    summary: FactChangeSummary
    summary_revalidate: Callable[[StaffActorContext], None] | None = None


class ResolveFactChange(Protocol):
    def __call__(
        self,
        *,
        actor: StaffActorContext,
        payload: FactChangeInput,
        request_id: UUID,
        effective_at: datetime,
    ) -> ResolvedFactChange: ...


class RequireFactMaintenance(Protocol):
    def __call__(
        self, actor: StaffActorContext, change_set: CanonicalFactChangeSetV1
    ) -> object: ...


@dataclass(slots=True)
class _Confirmation:
    owner: PrivateSourceOwner
    actor_binding: tuple[object, ...]
    credential: str
    idempotency_key: str
    expires_at: datetime
    summary: FactChangeSummary
    summary_revalidate: Callable[[StaffActorContext], None] | None = None
    invoked: bool = False
    rejected: bool = False


class PrepareAccessDenied(PermissionError):
    """Fixed prepare-stage metadata; never carries denied objects or authority details."""

    def __init__(self, code: Literal["prepare_resolution_denied", "prepare_maintenance_denied"]):
        if code not in {"prepare_resolution_denied", "prepare_maintenance_denied"}:
            raise ValueError("unknown prepare denial stage")
        self.code = code
        super().__init__(code)

    @property
    def reason(self) -> str:
        return {
            "prepare_resolution_denied": "本次所选目标或关联资料未能获准核对，未生成修改方案。",
            "prepare_maintenance_denied": "本次改动未通过当前维护授权核验，未生成可确认方案。",
        }[self.code]


class FactPreflightRejected(ContactProductUnavailable):
    """Explicit public domain feedback, never an underlying diagnostic or report."""

    def __init__(self, reason: str):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 800:
            raise ValueError("invalid public preflight reason")
        self.reason = reason
        super().__init__(reason)


class DirectoryFactProductService:
    """Prepare immutable changes, confirm once, recover by the existing Writer locator."""

    def __init__(
        self,
        *,
        boundary: SchoolFactChangeBoundary,
        resolve_change: ResolveFactChange,
        require_maintenance: RequireFactMaintenance,
        require_receipt: Callable[[StaffActorContext], object],
        plan_store: InMemorySchoolFactPlanStore,
        receipt_reference_for: ReceiptReferenceFor | None = None,
        receipt_fact_labels: Mapping[str, str] | None = None,
        receipt_reference_prefix: Literal["f2", "f3", "f4", "f6"] = "f2",
        receipt_uses_current_targets: bool = False,
        receipt_target_projection: ReceiptTargetProjection | None = None,
        preflight_rejection_formatter: Callable[[tuple[str, ...]], str | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._boundary = boundary
        self._resolve = resolve_change
        self._require = require_maintenance
        self._require_receipt = require_receipt
        self._plans = plan_store
        self._receipt_reference_for = receipt_reference_for
        self._receipt_fact_labels = dict(receipt_fact_labels or {})
        if not set(self._receipt_fact_labels) <= _MAINTENANCE_FACT_KINDS:
            raise ValueError("Receipt labels contain an unsupported fact kind")
        self._receipt_reference_prefix: Literal["f2", "f3", "f4", "f6"] = receipt_reference_prefix
        self._receipt_uses_current_targets = receipt_uses_current_targets
        self._receipt_target_projection = receipt_target_projection
        self._preflight_rejection_formatter = preflight_rejection_formatter
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._confirmations: dict[str, _Confirmation] = {}

    @staticmethod
    def _owner(actor: StaffActorContext, binding: str) -> PrivateSourceOwner:
        if not binding or len(binding) > 2000:
            raise ContactProductUnavailable("invalid session binding")
        return PrivateSourceOwner(
            school_id=str(actor.school_id),
            actor_user_id=actor.account_user_id,
            session_sha256=sha256(binding.encode()).hexdigest(),
        )

    @staticmethod
    def _actor_binding(actor: StaffActorContext) -> tuple[object, ...]:
        return (
            actor.person_id,
            actor.assignment_id,
            actor.assignment_version,
            actor.authorization_version,
        )

    def prepare(
        self,
        *,
        actor: StaffActorContext,
        session_binding: str,
        payload: FactChangeInput,
        request_id: UUID,
    ) -> FactProposal:
        owner = self._owner(actor, session_binding)
        now = self._clock()
        try:
            target = self._resolve(
                actor=actor,
                payload=payload,
                request_id=request_id,
                effective_at=payload.effective_at or now,
            )
        except PermissionError:
            raise PrepareAccessDenied("prepare_resolution_denied") from None
        try:
            self._require(actor, target.change_set)
        except PermissionError:
            raise PrepareAccessDenied("prepare_maintenance_denied") from None
        key = f"f2-facts:{uuid4()}"
        context = compose_command_context(actor, request_id, key)
        report = self._boundary.preflight(
            school_id=actor.school_id,
            change_set=target.change_set,
            context=context,
            session_binding=session_binding,
        )
        if not report.is_clean:
            if self._preflight_rejection_formatter is not None:
                codes = tuple(dict.fromkeys(issue.error_code for issue in report.issues))
                # The registered domain formatter sees bounded codes only, never
                # source identifiers, submitted values or the raw report.
                if len(codes) <= 64 and all(isinstance(code, str) for code in codes):
                    reason = self._preflight_rejection_formatter(codes)
                    if reason is not None:
                        raise FactPreflightRejected(reason)
            raise ContactProductUnavailable("当前修改未通过事实预检，请核对对象、引用和版本。")
        provenance = SchoolFactChangeProvenance(
            channel="admin",
            raw_source_sha256=target.change_set.source_sha256,
            template_version="admin_v3",
            mapping_version="f2_product_v1",
            mapping_config_sha256=sha256(b"f2-facts:approved-families:v1").hexdigest(),
            source_size_bytes=None,
            worksheet_names=(),
        )
        record = self._plans.put(
            owner=owner, change_set=target.change_set, provenance=provenance, preflight=report
        )
        try:
            try:
                self._require(actor, target.change_set)
            except PermissionError:
                raise PrepareAccessDenied("prepare_maintenance_denied") from None
            confirmation = self._boundary.prepare_confirmation(
                change_set=target.change_set,
                context=context,
                session_binding=session_binding,
                provenance=record.provenance,
            )
            if target.summary_revalidate is not None:
                target.summary_revalidate(actor)
            with self._lock:
                self.purge_expired()
                if len(self._confirmations) >= 128:
                    raise ContactProductUnavailable("temporary proposal capacity reached")
                self._confirmations[record.plan_ref] = _Confirmation(
                    owner,
                    self._actor_binding(actor),
                    confirmation.credential,
                    key,
                    confirmation.expires_at,
                    target.summary,
                    target.summary_revalidate,
                )
        except Exception:
            self._plans.delete(record.plan_ref, owner=owner)
            raise
        return FactProposal(
            proposal_ref=record.plan_ref, summary=target.summary, expires_at=confirmation.expires_at
        )

    def confirm(
        self, *, actor: StaffActorContext, session_binding: str, proposal_ref: str, request_id: UUID
    ) -> ContactReceipt:
        owner = self._owner(actor, session_binding)
        with self._lock:
            self.purge_expired()
            entry = self._confirmations.get(proposal_ref)
            if (
                entry is None
                or entry.owner != owner
                or entry.actor_binding != self._actor_binding(actor)
            ):
                raise ContactProductUnavailable("候选失效或当前身份不能访问，请查询原回执。")
            if entry.invoked:
                return self.receipt(
                    actor=actor,
                    session_binding=session_binding,
                    proposal_ref=proposal_ref,
                    request_id=request_id,
                )
            record = self._plans.get(proposal_ref, owner=owner)
            self._require(actor, record.change_set)
            if entry.summary_revalidate is not None:
                entry.summary_revalidate(actor)
            entry.invoked = True
        try:
            result = self._boundary.confirm(
                credential=entry.credential,
                change_set=record.change_set,
                context=compose_command_context(actor, request_id, entry.idempotency_key),
                session_binding=session_binding,
            )
        except SchoolFactChangeRejected:
            with self._lock:
                entry.rejected = True
            return ContactReceipt(
                reference=proposal_ref,
                status="rejected",
                message="正式修改被拒绝，请重新查询并预检。",
                request_id=str(request_id),
            )
        except Exception:
            return ContactReceipt(
                reference=proposal_ref,
                status="unknown",
                message="结果尚未查明，请查询此原回执；不要重新执行。",
                request_id=str(request_id),
            )
        finally:
            entry.credential = ""
            self._plans.retire(proposal_ref, owner=owner)
        return ContactReceipt(
            reference=proposal_ref,
            status="succeeded",
            message="事实已正式写入并保留审计。",
            changed_count=result.changed_count,
            target_version=result.target_version,
            request_id=str(request_id),
        )

    def proposal(
        self, *, actor: StaffActorContext, session_binding: str, proposal_ref: str
    ) -> FactProposal:
        owner = self._owner(actor, session_binding)
        with self._lock:
            self.purge_expired()
            entry = self._confirmations.get(proposal_ref)
            if (
                entry is None
                or entry.owner != owner
                or entry.actor_binding != self._actor_binding(actor)
                or entry.invoked
            ):
                raise ContactProductUnavailable("候选已失效或已经提交，请查询原回执。")
            record = self._plans.get(proposal_ref, owner=owner)
            self._require(actor, record.change_set)
            if entry.summary_revalidate is not None:
                entry.summary_revalidate(actor)
            return FactProposal(
                proposal_ref=proposal_ref, summary=entry.summary, expires_at=entry.expires_at
            )

    def require_receipt_access(self, actor: StaffActorContext) -> None:
        self._require_receipt(actor)

    def require_invoked(
        self, *, actor: StaffActorContext, session_binding: str, proposal_ref: str
    ) -> None:
        """Only the owning, actually invoked operation can continue a model task."""
        self.require_receipt_access(actor)
        owner = self._owner(actor, session_binding)
        with self._lock:
            self.purge_expired()
            entry = self._confirmations.get(proposal_ref)
            if (
                entry is None
                or not entry.invoked
                or entry.owner != owner
                or entry.actor_binding != self._actor_binding(actor)
            ):
                raise ContactProductUnavailable("original operation was not invoked")

    def receipt(
        self, *, actor: StaffActorContext, session_binding: str, proposal_ref: str, request_id: UUID
    ) -> ContactReceipt:
        self.require_receipt_access(actor)
        owner = self._owner(actor, session_binding)
        if not proposal_ref.startswith("sf3plan_") or len(proposal_ref) > 128:
            raise ContactProductUnavailable("invalid receipt reference")
        locator = sha256(proposal_ref.encode("ascii")).hexdigest()
        context = compose_command_context(actor, request_id, f"f2-result:{request_id}")
        if self._receipt_uses_current_targets:
            boundary = self._own_result_boundary(actor, session_binding)
            own_result = boundary.check_own_result_by_locator(
                context=context,
                session_binding=session_binding,
                locator_sha256=locator,
            )
            if own_result is None or own_result.locator_sha256 != locator:
                raise ContactProductUnavailable("receipt inaccessible")
            apply_result = own_result.result
        else:
            result = self._boundary.check_result_by_locator(
                school_id=actor.school_id,
                actor_user_id=actor.account_user_id,
                session_binding=session_binding,
                locator_sha256=locator,
                context=context,
            )
            if not result.found:
                raise ContactProductUnavailable("receipt inaccessible")
            apply_result = result.result
        if apply_result is None:
            with self._lock:
                self.purge_expired()
                entry = self._confirmations.get(proposal_ref)
                rejected = (
                    entry is not None
                    and entry.invoked
                    and entry.rejected
                    and entry.owner == owner
                    and entry.actor_binding == self._actor_binding(actor)
                )
            if rejected:
                return ContactReceipt(
                    reference=proposal_ref,
                    status="rejected",
                    message="原正式修改已明确被拒绝。",
                )
            return ContactReceipt(
                reference=proposal_ref,
                status="unknown",
                message="权威回执尚未确定，本次只查原操作。",
            )
        return ContactReceipt(
            reference=proposal_ref,
            status="succeeded",
            message="已从权威回执确认事实变更完成。",
            changed_count=apply_result.changed_count,
            target_version=apply_result.target_version,
        )

    def _own_result_boundary(
        self, actor: StaffActorContext, session_binding: str
    ) -> SchoolFactOwnResultBoundary:
        # The current read authority belongs to the Writer's own-result boundary.
        # The old maintenance receipt guard remains exclusive to the old path.
        self._owner(actor, session_binding)
        if (
            not isinstance(self._boundary, SchoolFactOwnResultBoundary)
            or self._receipt_reference_for is None
            or not self._receipt_fact_labels
        ):
            raise RuntimeError("own receipt recovery unavailable")
        return self._boundary

    def _own_receipt_view(
        self, actor: StaffActorContext, result: SchoolFactOwnResult, request_id: UUID
    ) -> OwnReceipt:
        if fullmatch(r"[0-9a-f]{64}", result.locator_sha256) is None or not result.targets:
            raise ContactProductUnavailable("receipt inaccessible")
        reference_for = self._receipt_reference_for
        if reference_for is None:
            raise RuntimeError("own receipt reference projection unavailable")
        targets: list[OwnReceiptTarget] = []
        for target in result.targets:
            projected = (
                self._receipt_target_projection(actor, target)
                if self._receipt_target_projection is not None
                else project_own_receipt_target(
                    actor,
                    target,
                    reference_for=reference_for,
                    labels=self._receipt_fact_labels,
                    prefix=self._receipt_reference_prefix,
                )
            )
            if (
                projected.fact_kind != target.fact_kind
                or projected.operation != target.operation
                or projected.target_version != target.target_version
            ):
                raise ContactProductUnavailable("receipt inaccessible")
            targets.append(projected)
        return OwnReceipt(
            reference="sf2receipt_" + result.locator_sha256,
            status="succeeded",
            message=(
                "本人历史正式回执确认原操作已提交成功；本次只读恢复，"
                "不证明当前目标仍存在或其字段可读，当前事实需另行查询核验。"
            ),
            changed_count=result.result.changed_count,
            target_version=result.result.target_version,
            request_id=str(request_id),
            confirmed_at=result.confirmed_at,
            targets=tuple(targets),
        )

    def _own_receipt_page_view(
        self, actor: StaffActorContext, page: SchoolFactOwnTargetPage, request_id: UUID
    ) -> OwnReceipt:
        terminal = page.terminal
        reference_for = self._receipt_reference_for
        if (
            reference_for is None
            or fullmatch(r"[0-9a-f]{64}", terminal.locator_sha256) is None
            or len(page.items) > page.next_offset - page.offset
            or not terminal.result.replayed
            or terminal.result.changed_count != terminal.target_count
        ):
            raise ContactProductUnavailable("receipt inaccessible")
        projected: list[OwnReceiptTarget] = []
        for target in page.items:
            item = (
                self._receipt_target_projection(actor, target)
                if self._receipt_target_projection is not None
                else project_own_receipt_target(
                    actor,
                    target,
                    reference_for=reference_for,
                    labels=self._receipt_fact_labels,
                    prefix=self._receipt_reference_prefix,
                )
            )
            if (
                item.fact_kind != target.fact_kind
                or item.operation != target.operation
                or item.target_version != target.target_version
            ):
                raise ContactProductUnavailable("receipt inaccessible")
            projected.append(item)
        return OwnReceipt(
            reference="sf2receipt_" + terminal.locator_sha256,
            status="succeeded",
            message=(
                "本人原操作已确认成功；此处只列出本页当前获准查看的目标。"
                "未显示的目标不代表原操作未执行，当前事实需另行查询。"
            ),
            changed_count=terminal.result.changed_count,
            target_version=terminal.result.target_version,
            request_id=str(request_id),
            confirmed_at=terminal.confirmed_at,
            targets=tuple(projected),
            target_page=OwnReceiptTargetPage(
                offset=page.offset,
                next_offset=page.next_offset,
                target_count=terminal.target_count,
                has_more=page.has_more,
            ),
        )

    def own_receipts(
        self,
        *,
        actor: StaffActorContext,
        session_binding: str,
        request_id: UUID,
        limit: int = 5,
    ) -> OwnReceiptPage:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("invalid own receipt limit")
        boundary = self._own_result_boundary(actor, session_binding)
        try:
            page = boundary.list_own_results(
                context=compose_command_context(actor, request_id, f"f2-own-results:{request_id}"),
                session_binding=session_binding,
                limit=limit,
            )
        except (SchoolFactChangeRejected, PermissionError):
            raise ContactProductUnavailable("receipt inaccessible") from None
        if len(page.items) > limit or len({item.locator_sha256 for item in page.items}) != len(
            page.items
        ):
            raise ContactProductUnavailable("receipt inaccessible")
        return OwnReceiptPage(
            items=tuple(self._own_receipt_view(actor, item, request_id) for item in page.items),
            has_more=page.has_more,
        )

    def own_receipt(
        self,
        *,
        actor: StaffActorContext,
        session_binding: str,
        request_id: UUID,
        receipt_ref: str,
        target_offset: int = 0,
        target_limit: int = 20,
    ) -> OwnReceipt:
        if (
            type(receipt_ref) is not str
            or fullmatch(r"sf2receipt_[0-9a-f]{64}", receipt_ref) is None
        ):
            raise ValueError("invalid own receipt reference")
        if (
            type(target_offset) is not int
            or not 0 <= target_offset <= 50_000
            or type(target_limit) is not int
            or not 1 <= target_limit <= 20
        ):
            raise ValueError("invalid receipt target page")
        boundary = self._own_result_boundary(actor, session_binding)
        locator_sha256 = receipt_ref.removeprefix("sf2receipt_")
        try:
            result = boundary.check_own_result_by_locator(
                context=compose_command_context(actor, request_id, f"f2-own-result:{request_id}"),
                session_binding=session_binding,
                locator_sha256=locator_sha256,
            )
        except (SchoolFactChangeRejected, PermissionError):
            # The earlier detail route requires current rights to every target.
            # A separate current own-terminal duty can still release the
            # operation status, while its page route independently checks each
            # detail before disclosure.
            result = None
        if result is None:
            paged = getattr(boundary, "read_own_target_page", None)
            if not callable(paged):
                raise ContactProductUnavailable("receipt inaccessible")
            try:
                page = cast(SchoolFactOwnTargetPage | None, paged(
                    context=compose_command_context(
                        actor, request_id, f"f7-own-target-page:{request_id}"
                    ),
                    session_binding=session_binding,
                    locator_sha256=locator_sha256,
                    offset=target_offset,
                    limit=target_limit,
                ))
            except (SchoolFactChangeRejected, PermissionError):
                raise ContactProductUnavailable("receipt inaccessible") from None
            if page is None or page.terminal.locator_sha256 != locator_sha256:
                raise ContactProductUnavailable("receipt inaccessible")
            return self._own_receipt_page_view(actor, page, request_id)
        if result.locator_sha256 != locator_sha256:
            raise ContactProductUnavailable("receipt inaccessible")
        return self._own_receipt_view(actor, result, request_id)

    def own_receipt_original(
        self,
        *,
        actor: StaffActorContext,
        session_binding: str,
        request_id: UUID,
        receipt_ref: str,
        target_offset: int = 0,
        target_limit: int = 3,
    ) -> tuple[OwnReceipt, SchoolFactOwnResult]:
        """Exact committed fields, separately authorized by the existing Writer reader."""
        if (
            type(receipt_ref) is not str
            or fullmatch(r"sf2receipt_[0-9a-f]{64}", receipt_ref) is None
        ):
            raise ValueError("invalid own receipt reference")
        if (
            type(target_offset) is not int
            or not 0 <= target_offset <= 50_000
            or type(target_limit) is not int
            or not 1 <= target_limit <= 3
        ):
            raise ValueError("invalid original field page")
        boundary = self._own_result_boundary(actor, session_binding)
        locator = receipt_ref.removeprefix("sf2receipt_")
        result = boundary.check_own_result_by_locator(
            context=compose_command_context(actor, request_id, f"f2-own-original:{request_id}"),
            session_binding=session_binding,
            locator_sha256=locator,
            original_fields={
                "teaching_assignment": (
                    "assignment_source_key",
                    "offering_source_key",
                    "teaching_role",
                )
            },
            target_offset=target_offset,
            target_limit=target_limit,
        )
        if result is None or result.locator_sha256 != locator:
            raise ContactProductUnavailable("receipt inaccessible")
        return self._own_receipt_view(actor, result, request_id), result

    def purge_expired(self, *, now: datetime | None = None) -> int:
        now = now or self._clock()
        with self._lock:
            refs = [ref for ref, item in self._confirmations.items() if item.expires_at <= now]
            for ref in refs:
                self._confirmations.pop(ref).credential = ""
        return len(refs)
