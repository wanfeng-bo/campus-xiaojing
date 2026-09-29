import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal, Never

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import JsonValue

from app.interfaces.http.errors import ApiError, HttpErrorResponse
from app.interfaces.http.framework_request import (
    FrameworkRequestContext,
    FrameworkRequestPresence,
    get_framework_request_context,
    get_framework_request_presence,
)
from app.interfaces.http.school_fact_schemas import (
    OpaqueRef,
    SchoolFactAdaptedPreflightRequest,
    SchoolFactAdminPreflightRequest,
    SchoolFactApplyResponse,
    SchoolFactChangeSummaryResponse,
    SchoolFactConfirmationResponse,
    SchoolFactConfirmRequest,
    SchoolFactExcelPreflightRequest,
    SchoolFactFieldDiffResponse,
    SchoolFactIssueResponse,
    SchoolFactPackagePreflightRequest,
    SchoolFactPlanResponse,
    SchoolFactResultResponse,
    SchoolFactSegmentSummaryResponse,
    SchoolFactSelectedPreflightRequest,
    SchoolFactSourceResponse,
    SchoolFactStatusDecisionResponse,
    SchoolFactUploadRequest,
)
from app.platform.authorization.secure_reader_contracts import SecureReadDenied
from app.platform.contracts import CommandExecutionContext, SchoolId, StaffActorContext
from app.platform.identity.execution_context import compose_command_context
from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from app.platform.master_data_imports.excel import ImportWorkbookRejected
from app.platform.master_data_imports.ports import (
    PrivateImportSourceAccessDenied,
    PrivateImportSourceExpired,
    PrivateImportSourceStore,
    PrivateSourceOwner,
    SchoolFactPlanStore,
    StoredSchoolFactPlan,
)
from app.platform.master_data_imports.school_facts_excel import (
    SCHOOL_FACT_ADMIN_MAPPING_CONFIG_SHA256,
    SCHOOL_FACT_ADMIN_MAPPING_VERSION,
    SCHOOL_FACT_SHEET_SPECS,
    SCHOOL_FACT_TEMPLATE_VERSION,
    SchoolFactAdminChangeV1,
    SchoolFactAdminPlanBuilder,
    SchoolFactWorkbookIssue,
    SchoolFactWorkbookParser,
)
from app.platform.master_data_imports.school_facts_package import (
    FullScopeInputManifestV5,
    FullScopeInputRejected,
    FullScopePackageBuilder,
    FullScopeSourcePart,
    parse_full_scope_fact_json_source,
    parse_full_scope_workbook_source,
)
from app.platform.master_data_imports.school_facts_replacement import (
    PackageMaintenancePreview,
    prepare_selected_replacement,
)
from app.platform.master_data_imports.source_mapping import (
    ResolvedSourceMappingProfile,
    SchoolFactSourceAdapter,
    SourceMappingError,
    require_adapted_source_capacity,
)
from app.platform.master_data_imports.status_mappings import StatusMappingResult
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.change_boundary import SchoolFactChangeBoundary
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    PreflightIssueV1,
    safe_content_payload,
)
from app.platform.school_facts.ports import (
    SchoolFactChangeProvenance,
    SchoolFactPolicyTextAttachment,
    SchoolFactRowProvenance,
)


@dataclass(frozen=True, slots=True)
class SchoolFactRequestIdentity:
    """只由服务端身份和授权组合层提供。"""

    actor: StaffActorContext
    session_binding: str
    authorized_actions: frozenset[str]
    scope: Literal["normal", "school_reactivation_only"] = "normal"


type IdentityDependency = Callable[..., SchoolFactRequestIdentity]
type MappingProfileResolver = Callable[
    [SchoolId, str, tuple[str, ...]],
    ResolvedSourceMappingProfile,
]


def _owner(identity: SchoolFactRequestIdentity) -> PrivateSourceOwner:
    return PrivateSourceOwner(
        school_id=str(identity.actor.school_id),
        actor_user_id=identity.actor.account_user_id,
        session_sha256=sha256(identity.session_binding.encode("utf-8")).hexdigest(),
    )


def _read_operation_context(
    identity: SchoolFactRequestIdentity,
    request_context: FrameworkRequestContext,
    operation: Literal["preflight", "result"],
) -> CommandExecutionContext:
    return compose_command_context(
        identity.actor,
        request_context.request_id,
        f"school-facts:{operation}:{request_context.request_id}",
    )


def _require_authority(identity: SchoolFactRequestIdentity) -> None:
    if "school_facts.manage" not in identity.authorized_actions:
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="school_facts.forbidden",
            message="当前履职身份不能管理学校基础数据。",
        )


def _raise_scope_forbidden() -> Never:
    raise ApiError(
        status_code=status.HTTP_403_FORBIDDEN,
        code="school_facts.forbidden",
        message="当前履职身份不能执行这个学校基础数据操作。",
    )


def _require_normal_scope(identity: SchoolFactRequestIdentity) -> None:
    if identity.scope != "normal":
        _raise_scope_forbidden()


def _is_single_school_reactivation(change_set: CanonicalFactChangeSetV1) -> bool:
    changes = change_set.changes
    return (
        len(changes) == 1
        and changes[0].fact_kind == "school"
        and changes[0].operation == "reactivate"
    )


def _require_plan_scope(
    identity: SchoolFactRequestIdentity,
    *,
    change_set: CanonicalFactChangeSetV1,
    channel: str,
) -> None:
    if identity.scope == "normal":
        return
    if channel != "admin" or not _is_single_school_reactivation(change_set):
        _raise_scope_forbidden()


def _idempotency_key(value: str | None) -> str:
    if value is None or not value.strip() or len(value.encode("utf-8")) > 255:
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="school_facts.request_invalid",
            message="本次操作标识无效，请刷新后重试。",
        )
    return value


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _require_empty(presence: FrameworkRequestPresence) -> None:
    if presence.has_body or presence.has_query_parameters:
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="school_facts.request_invalid",
            message="这个操作不接受附加内容。",
        )


def _raise_channel_error(exc: Exception) -> Never:
    if isinstance(exc, ApiError):
        raise exc
    if isinstance(exc, (PermissionError, SecureReadDenied)):
        _raise_scope_forbidden()
    if isinstance(exc, (KeyError, PrivateImportSourceExpired, PrivateImportSourceAccessDenied)):
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="school_facts.resource_unavailable",
            message="导入来源或预检计划不存在、已过期或不可访问。",
        ) from None
    if isinstance(exc, SourceMappingError) and exc.code == "SOURCE_SET_CAPACITY_EXCEEDED":
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="school_facts.source_set_capacity_exceeded",
            message="来源文件数量或总大小超过单次预检上限。",
        ) from None
    if isinstance(exc, SchoolFactWorkbookIssue):
        error_codes = {
            "STATUS_MAPPING_REQUIRED": "school_facts.status_mapping_required",
            "STATUS_MAPPING_AMBIGUOUS": "school_facts.status_mapping_ambiguous",
        }
        code = error_codes.get(exc.code)
        if code is None:
            raise ApiError(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="school_facts.import_rejected",
                message="导入内容未通过格式或安全检查。",
            ) from None
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=code,
            message=(
                f"{f'{exc.source_slot}/' if exc.source_slot else ''}{exc.sheet_code} "
                f"第 {exc.row_number} 行的 {exc.field_code} "
                f"需要按服务端状态字典 {exc.mapping_version} 处理。"
            ),
        ) from None
    if isinstance(
        exc, (FullScopeInputRejected, ImportWorkbookRejected, binascii.Error, ValueError)
    ):
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="school_facts.import_rejected",
            message="导入内容未通过格式或安全检查。",
        ) from None
    if isinstance(exc, SchoolFactChangeRejected):
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="school_facts.change_rejected",
            message="学校基础数据已变化或确认已失效，请重新预检。",
        ) from None
    raise ApiError(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="school_facts.unavailable",
        message="学校基础数据通道暂时不可用。",
    ) from None


def _plan_response(
    record: StoredSchoolFactPlan,
    *,
    status_decisions: tuple[StatusMappingResult, ...] = (),
    replacement_preview: tuple = (),
    maintenance_preview: PackageMaintenancePreview | None = None,
) -> SchoolFactPlanResponse:
    return SchoolFactPlanResponse(
        plan_ref=record.plan_ref,
        plan_sha256=record.preflight.plan_sha256,
        is_clean=record.preflight.is_clean,
        issues=tuple(_issue_response(record, issue) for issue in record.preflight.issues),
        changes=tuple(
            SchoolFactChangeSummaryResponse(
                operation=change.operation,
                fact_kind=change.fact_kind,
                source_key=change.source_key,
                expected_version=change.expected_version,
                sheet_code=change.origin.sheet_code,
                row_number=change.origin.row_number,
                effective_at=change.effective_at,
                correction_reason=change.correction_reason,
                cause_source_key=change.cause_source_key,
                cause_version=change.cause_version,
                replacement_segments=tuple(
                    SchoolFactSegmentSummaryResponse(
                        effective_at=segment.effective_at,
                        valid_to=segment.valid_to,
                        cause_source_key=segment.cause_source_key,
                        cause_version=segment.cause_version,
                        fields=tuple(
                            SchoolFactFieldDiffResponse(
                                field_code=field,
                                new_value=_safe_diff_value(value),
                            )
                            for field, value in sorted(
                                (safe_content_payload(segment.content) or {}).items()
                            )
                        ),
                    )
                    for segment in (change.replacement_segments or ())
                ),
                fields=tuple(
                    SchoolFactFieldDiffResponse(
                        field_code=field_code,
                        new_value=_safe_diff_value(value),
                    )
                    for field_code, value in sorted(
                        (safe_content_payload(change.content) or {}).items()
                    )
                ),
            )
            for change in record.change_set.changes
        ),
        status_decisions=tuple(
            _status_decision_response(record, decision) for decision in status_decisions
        ),
        replacement_preview=replacement_preview,
        preview_status=(maintenance_preview.status if maintenance_preview else "unavailable"),
        preview_issues=maintenance_preview.issues if maintenance_preview else (),
        change_preview=maintenance_preview.changes if maintenance_preview else (),
    )


def _safe_diff_value(value: object) -> JsonValue | tuple[str, ...]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return tuple(value)
        return [_safe_diff_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _safe_diff_value(item) for key, item in value.items()}
    raise ValueError("学校事实差异包含未登记的值类型。")


def _lineage_for_target(
    record: StoredSchoolFactPlan,
    *,
    origin_code: str,
    row_number: int,
) -> SchoolFactRowProvenance | None:
    change = next(
        (
            item
            for item in record.change_set.changes
            if item.origin.sheet_code == origin_code and item.origin.row_number == row_number
        ),
        None,
    )
    if change is None:
        return None
    return next(
        (
            item
            for item in record.provenance.row_lineage
            if item.fact_kind == change.fact_kind and item.fact_source_key == change.source_key
        ),
        None,
    )


def _safe_source_field(lineage: SchoolFactRowProvenance, target_field_code: str) -> str:
    target_fields = lineage.target_field_codes
    source_fields = lineage.source_field_codes
    try:
        index = target_fields.index(target_field_code)
    except ValueError:
        return target_field_code
    return source_fields[index]


def _issue_response(
    record: StoredSchoolFactPlan, issue: PreflightIssueV1
) -> SchoolFactIssueResponse:
    sheet_code = issue.sheet_code
    row_number = issue.row_number
    column_code = issue.column_code
    lineage = _lineage_for_target(
        record,
        origin_code=sheet_code,
        row_number=row_number,
    )
    if lineage is None:
        return SchoolFactIssueResponse(
            source_slot="admin",
            sheet_code=sheet_code,
            row_number=row_number,
            column_code=column_code,
            error_code=issue.error_code,
        )
    return SchoolFactIssueResponse(
        source_slot=lineage.source_slot,
        sheet_code=lineage.source_sheet_code,
        row_number=lineage.source_row_number,
        column_code=_safe_source_field(lineage, column_code),
        error_code=issue.error_code,
    )


def _status_decision_response(
    record: StoredSchoolFactPlan,
    decision: StatusMappingResult,
) -> SchoolFactStatusDecisionResponse:
    if decision.source_slot is not None and decision.row_number is not None:
        return SchoolFactStatusDecisionResponse(
            source_slot=decision.source_slot,
            sheet_code=decision.sheet_code,
            row_number=decision.row_number,
            field_code=decision.field_code,
            original_status_value=decision.original_value,
            standard_value=decision.standard_value,
            mapping_version=decision.mapping_version,
        )
    if decision.row_number is None:
        raise ValueError("状态映射决定缺少安全行坐标。")
    spec = SCHOOL_FACT_SHEET_SPECS.get(decision.sheet_code)
    if spec is None:
        raise ValueError("状态映射决定缺少登记目标页。")
    lineage = _lineage_for_target(
        record,
        origin_code=spec.origin_code,
        row_number=decision.row_number,
    )
    if lineage is None:
        raise ValueError("状态映射决定缺少来源血缘。")
    return SchoolFactStatusDecisionResponse(
        source_slot=lineage.source_slot,
        sheet_code=lineage.source_sheet_code,
        row_number=lineage.source_row_number,
        field_code=_safe_source_field(lineage, decision.field_code),
        original_status_value=decision.original_value,
        standard_value=decision.standard_value,
        mapping_version=decision.mapping_version,
    )


def create_school_fact_router(
    *,
    change_service: SchoolFactChangeBoundary,
    source_store: PrivateImportSourceStore,
    plan_store: SchoolFactPlanStore,
    identity_dependency: IdentityDependency,
    mapping_profile_resolver: MappingProfileResolver | None = None,
    enable_package: bool = False,
    package_only: bool = False,
    result_only: bool = False,
    authorize_package: Callable[[SchoolFactRequestIdentity, CanonicalFactChangeSetV1], None]
    | None = None,
    authorize_package_result: Callable[[SchoolFactRequestIdentity], Callable[[], None]]
    | None = None,
    package_replacement: object | None = None,
    package_source_adapter: object | None = None,
    authorize_package_selection: Callable | None = None,
    clock: Callable[[], datetime] | None = None,
) -> APIRouter:
    """Retained isolated import routes, or exact-authorized F7 package-only Product routes."""

    if result_only and not package_only:
        raise ValueError(
            "Original package result-only route requires package-only identity checks."
        )
    if package_only and (
        not enable_package or authorize_package is None or authorize_package_result is None
    ):
        raise ValueError("Package-only mounting requires exact change and result authorization.")

    def require_authority(identity: SchoolFactRequestIdentity) -> None:
        if package_only:
            if "school_facts.package.submit" not in identity.authorized_actions:
                _raise_scope_forbidden()
            _require_normal_scope(identity)
        else:
            _require_authority(identity)

    def require_package(identity, change_set, channel="package") -> None:
        if package_only and channel != "package":
            _raise_scope_forbidden()
        if channel == "package" and authorize_package is not None:
            authorize_package(identity, change_set)

    def require_selection(identity, record):
        if record.provenance.replacement_targets:
            if authorize_package_selection is None:
                raise PermissionError("完整替换的当前读取范围不可核验。")
            authorize_package_selection(identity, record.provenance.replacement_targets)

    def current_package_preview(identity, change_set, parts=()):
        method = getattr(package_replacement, "preview", None)
        if not callable(method):
            return PackageMaintenancePreview(
                "unavailable", ("PACKAGE_PREVIEW_UNAVAILABLE",), (), lambda: None
            )
        reviews = tuple(p.domain_review for p in parts if p.domain_review is not None)
        return (
            method(identity.actor, change_set, source_reviews=reviews)
            if reviews
            else method(identity.actor, change_set)
        )

    def read_package_parts(identity, payload):
        from app.composition.school_fact_package_sources import DOMAIN_SOURCE_VERSIONS

        owner = _owner(identity)
        parts = []
        for declared, reference in zip(payload.manifest.sources, payload.sources, strict=True):
            source = source_store.describe(reference.source_ref, owner=owner)
            if (
                source.source_sha256 != declared.source_sha256
                or source.size_bytes != declared.size_bytes
            ):
                raise FullScopeInputRejected("SOURCE_CONTENT_MISMATCH")
            with source_store.open(reference.source_ref, owner=owner) as stream:
                raw = stream.read(declared.size_bytes + 1)
            if declared.purpose == "policy_text":
                if not source.filename_summary.lower().endswith(".txt"):
                    raise FullScopeInputRejected("SOURCE_FORMAT_INVALID")
                parts.append(FullScopeSourcePart(declared.logical_source_code, raw, (), ()))
            elif declared.mapping_version in DOMAIN_SOURCE_VERSIONS:
                if package_source_adapter is None:
                    raise FullScopeInputRejected("SOURCE_DOMAIN_ADAPTER_UNAVAILABLE")
                parts.append(
                    package_source_adapter.parse(
                        actor=identity.actor,
                        declared=declared,
                        raw=raw,
                        base_version=payload.manifest.base_version,
                    )
                )
            elif source.filename_summary.lower().endswith(".xlsx"):
                parts.append(
                    parse_full_scope_workbook_source(
                        declared, raw, source=source, base_version=payload.manifest.base_version
                    )
                )
            else:
                if not source.filename_summary.lower().endswith(".json"):
                    raise FullScopeInputRejected("SOURCE_FORMAT_INVALID")
                parts.append(
                    parse_full_scope_fact_json_source(
                        declared, raw, allow_empty=payload.manifest.mode == "replace"
                    )
                )
        return tuple(parts)

    def replay_package_parts(identity, record):
        if record.package_request_json is None:
            return ()
        payload = SchoolFactPackagePreflightRequest.model_validate_json(record.package_request_json)
        parts = read_package_parts(identity, payload)
        replacement = None
        if payload.manifest.mode == "replace":
            replacement = prepare_selected_replacement(
                manifest=payload.manifest,
                parts=parts,
                read_snapshot=lambda kind, key, at: package_replacement.read(
                    identity.actor, kind, key, at
                ),
                prepare_end=lambda old: package_replacement.end(identity.actor, old),
            )
        rebuilt = FullScopePackageBuilder().build(
            manifest=payload.manifest,
            parts=parts,
            replacement_targets=replacement.targets if replacement else (),
            generated_changes=replacement.generated_changes if replacement else (),
        )
        if rebuilt.change_set != record.change_set:
            raise FullScopeInputRejected("PACKAGE_DOMAIN_REPLAY_CHANGED")
        require_package(identity, rebuilt.change_set)
        if replacement is not None:
            replacement.revalidate()
        return parts

    def require_complete_preview(identity, record):
        if package_only and record.provenance.channel == "package":
            parts = replay_package_parts(identity, record)
            review = current_package_preview(identity, record.change_set, parts)
            if not record.preflight.is_clean or review.status != "complete":
                raise FullScopeInputRejected("PACKAGE_COMPLETE_PREVIEW_REQUIRED")
            review.revalidate()
            return review
        return None

    now = clock or (lambda: datetime.now(UTC))
    router = APIRouter(
        prefix=("/api/v1/school-fact-product/imports" if package_only else "/api/v1/school-facts"),
        tags=["school-facts"],
        responses={
            403: {"model": HttpErrorResponse},
            404: {"model": HttpErrorResponse},
            409: {"model": HttpErrorResponse},
            422: {"model": HttpErrorResponse},
            503: {"model": HttpErrorResponse},
        },
    )

    @router.post("/sources", response_model=SchoolFactSourceResponse, status_code=201)
    def upload_source(  # pyright: ignore[reportUnusedFunction]
        payload: SchoolFactUploadRequest,
        response: Response,
        identity: Annotated[SchoolFactRequestIdentity, Depends(identity_dependency)],
    ) -> SchoolFactSourceResponse:
        _no_store(response)
        require_authority(identity)
        _require_normal_scope(identity)
        try:
            content = base64.b64decode(payload.content_base64, validate=True)
            source = source_store.put(
                filename_summary=payload.filename_summary,
                content=content,
                owner=_owner(identity),
            )
        except Exception as exc:
            _raise_channel_error(exc)
        return SchoolFactSourceResponse(
            source_ref=source.opaque_ref,
            source_sha256=source.source_sha256,
            expires_at=now() + timedelta(hours=24),
        )

    @router.delete("/sources/{source_ref}", status_code=status.HTTP_204_NO_CONTENT)
    def abandon_source(  # pyright: ignore[reportUnusedFunction]
        source_ref: OpaqueRef,
        response: Response,
        identity: Annotated[SchoolFactRequestIdentity, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> Response:
        """放弃尚在候选通道内的私有原始文件；不接触事实或长期血缘。"""

        _no_store(response)
        require_authority(identity)
        _require_normal_scope(identity)
        _require_empty(presence)
        try:
            source_store.delete(source_ref, owner=_owner(identity))
        except Exception as exc:
            _raise_channel_error(exc)
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @router.post("/plans/excel", response_model=SchoolFactPlanResponse)
    def preflight_excel(  # pyright: ignore[reportUnusedFunction]
        payload: SchoolFactExcelPreflightRequest,
        response: Response,
        identity: Annotated[SchoolFactRequestIdentity, Depends(identity_dependency)],
        request_context: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> SchoolFactPlanResponse:
        _no_store(response)
        require_authority(identity)
        _require_normal_scope(identity)
        owner = _owner(identity)
        try:
            source = source_store.describe(payload.source_ref, owner=owner)
            with source_store.open(payload.source_ref, owner=owner) as stream:
                parsed = SchoolFactWorkbookParser().parse(
                    stream,
                    source=source,
                    base_version=payload.base_version,
                )
            preflight = change_service.preflight(
                school_id=identity.actor.school_id,
                change_set=parsed.change_set,
                context=_read_operation_context(identity, request_context, "preflight"),
                session_binding=identity.session_binding,
            )
            record = plan_store.put(
                owner=owner,
                change_set=parsed.change_set,
                provenance=SchoolFactChangeProvenance(
                    channel="excel",
                    raw_source_sha256=parsed.raw_source_sha256,
                    template_version=parsed.template_version,
                    mapping_version=parsed.mapping_version,
                    mapping_config_sha256=parsed.mapping_config_sha256,
                    source_size_bytes=parsed.standard_artifact_size_bytes,
                    worksheet_names=tuple(SCHOOL_FACT_SHEET_SPECS),
                    source_manifest=parsed.source_manifest,
                    row_lineage=parsed.row_lineage,
                ),
                preflight=preflight,
                source_refs=(source.opaque_ref,),
            )
        except Exception as exc:
            _raise_channel_error(exc)
        return _plan_response(
            record,
            status_decisions=parsed.status_decisions,
        )

    @router.post("/plans/admin", response_model=SchoolFactPlanResponse)
    def preflight_admin(  # pyright: ignore[reportUnusedFunction]
        payload: SchoolFactAdminPreflightRequest,
        response: Response,
        identity: Annotated[SchoolFactRequestIdentity, Depends(identity_dependency)],
        request_context: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> SchoolFactPlanResponse:
        _no_store(response)
        require_authority(identity)
        try:
            has_reactivation = any(item.operation == "reactivate" for item in payload.changes)
            if has_reactivation:
                if len(payload.changes) != 1:
                    if identity.scope == "school_reactivation_only":
                        _raise_scope_forbidden()
                    raise ValueError("显式重新启用必须是独立计划。")
                item = payload.changes[0]
                if identity.scope == "school_reactivation_only" and item.fact_kind != "school":
                    _raise_scope_forbidden()
                origin_by_kind = {
                    spec.fact_kind: spec.origin_code
                    for spec in SCHOOL_FACT_SHEET_SPECS.values()
                    if spec.fact_kind is not None
                }
                origin_code = origin_by_kind.get(item.fact_kind)
                if origin_code is None:
                    raise ValueError("管理台事实类别未登记。")
                change_set = SchoolFactAdminPlanBuilder().build(
                    base_version=payload.base_version,
                    changes=(
                        CanonicalFactChangeV1.model_validate(
                            {
                                **item.model_dump(
                                    mode="python",
                                    exclude_none=True,
                                    exclude_unset=True,
                                ),
                                "schema_version": item.schema_version,
                                "origin": {"sheet_code": origin_code, "row_number": 2},
                            }
                        ),
                    ),
                )
            else:
                change_set = SchoolFactAdminPlanBuilder().build_from_inputs(
                    base_version=payload.base_version,
                    inputs=tuple(
                        SchoolFactAdminChangeV1.model_validate(
                            item.model_dump(mode="python", exclude_none=True, exclude_unset=True)
                        )
                        for item in payload.changes
                    ),
                )
            _require_plan_scope(identity, change_set=change_set, channel="admin")
            preflight = change_service.preflight(
                school_id=identity.actor.school_id,
                change_set=change_set,
                context=_read_operation_context(identity, request_context, "preflight"),
                session_binding=identity.session_binding,
            )
            record = plan_store.put(
                owner=_owner(identity),
                change_set=change_set,
                provenance=SchoolFactChangeProvenance(
                    channel="admin",
                    raw_source_sha256=change_set.source_sha256,
                    template_version=SCHOOL_FACT_TEMPLATE_VERSION,
                    mapping_version=SCHOOL_FACT_ADMIN_MAPPING_VERSION,
                    mapping_config_sha256=SCHOOL_FACT_ADMIN_MAPPING_CONFIG_SHA256,
                    source_size_bytes=None,
                    worksheet_names=(),
                ),
                preflight=preflight,
            )
        except Exception as exc:
            _raise_channel_error(exc)
        return _plan_response(record)

    @router.post("/plans/adapted", response_model=SchoolFactPlanResponse)
    def preflight_adapted(  # pyright: ignore[reportUnusedFunction]
        payload: SchoolFactAdaptedPreflightRequest,
        response: Response,
        identity: Annotated[SchoolFactRequestIdentity, Depends(identity_dependency)],
        request_context: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> SchoolFactPlanResponse:
        _no_store(response)
        require_authority(identity)
        _require_normal_scope(identity)
        if mapping_profile_resolver is None:
            raise ApiError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="school_facts.mapping_unavailable",
                message="当前实例尚未登记这份学校原表的字段映射。",
            )
        owner = _owner(identity)
        try:
            bindings = {item.source_slot: item.source_ref for item in payload.sources}
            source_refs = tuple(item.source_ref for item in payload.sources)
            if len(bindings) != len(payload.sources) or len(set(source_refs)) != len(source_refs):
                raise ValueError("来源逻辑槽和引用不能重复。")
            described_sources: dict[str, ProtectedImportSourceRef] = {}
            for source_ref in source_refs:
                described_sources[source_ref] = source_store.describe(source_ref, owner=owner)
            require_adapted_source_capacity(tuple(described_sources.values()))
            sources: dict[str, tuple[ProtectedImportSourceRef, bytes]] = {}
            for source_ref, source in described_sources.items():
                with source_store.open(source_ref, owner=owner) as stream:
                    content = stream.read()
                sources[source_ref] = (source, content)
            source_slots = tuple(sorted(bindings))
            resolved_profile = mapping_profile_resolver(
                identity.actor.school_id,
                payload.mapping_version,
                source_slots,
            )
            if (
                resolved_profile.school_id != str(identity.actor.school_id)
                or resolved_profile.profile.version != payload.mapping_version
                or resolved_profile.source_slots != source_slots
            ):
                raise ValueError("来源映射登记绑定不一致。")
            profile = resolved_profile.bind_source_refs(bindings)
            parsed = SchoolFactSourceAdapter().adapt(
                sources=sources,
                profile=profile,
                base_version=payload.base_version,
            )
            preflight = change_service.preflight(
                school_id=identity.actor.school_id,
                change_set=parsed.change_set,
                context=_read_operation_context(identity, request_context, "preflight"),
                session_binding=identity.session_binding,
            )
            record = plan_store.put(
                owner=owner,
                change_set=parsed.change_set,
                provenance=SchoolFactChangeProvenance(
                    channel="excel",
                    raw_source_sha256=parsed.raw_source_sha256,
                    template_version=parsed.template_version,
                    mapping_version=parsed.mapping_version,
                    mapping_config_sha256=resolved_profile.config_sha256,
                    source_size_bytes=parsed.standard_artifact_size_bytes,
                    worksheet_names=tuple(SCHOOL_FACT_SHEET_SPECS),
                    source_manifest=parsed.source_manifest,
                    row_lineage=parsed.row_lineage,
                ),
                preflight=preflight,
                source_refs=source_refs,
            )
        except Exception as exc:
            _raise_channel_error(exc)
        return _plan_response(
            record,
            status_decisions=parsed.status_decisions,
        )

    package_route = (
        router.post("/plans/package", response_model=SchoolFactPlanResponse)
        if enable_package
        else lambda endpoint: endpoint
    )

    @package_route
    def preflight_package(  # pyright: ignore[reportUnusedFunction]
        payload: SchoolFactPackagePreflightRequest,
        response: Response,
        identity: Annotated[SchoolFactRequestIdentity, Depends(identity_dependency)],
        request_context: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> SchoolFactPlanResponse:
        """Only validated private F7 input enters the existing confirmation/Writer path."""

        _no_store(response)
        require_authority(identity)
        _require_normal_scope(identity)
        if payload.manifest.school_id != identity.actor.school_id:
            _raise_scope_forbidden()
        owner = _owner(identity)
        try:
            # Replacement requires an authorized complete selected-set snapshot;
            # omission in a file is never sufficient evidence to end a fact.
            if payload.manifest.mode == "replace" and (
                package_replacement is None or authorize_package_selection is None
            ):
                raise FullScopeInputRejected("REPLACEMENT_SNAPSHOT_NOT_AVAILABLE")
            if tuple(item.source_slot for item in payload.sources) != tuple(
                item.logical_source_code for item in payload.manifest.sources
            ):
                raise FullScopeInputRejected("SOURCE_SET_MISMATCH")
            source_refs = tuple(item.source_ref for item in payload.sources)
            if len(set(source_refs)) != len(source_refs):
                raise FullScopeInputRejected("SOURCE_REF_DUPLICATE")
            parts = read_package_parts(identity, payload)
            replacement = None
            if payload.manifest.mode == "replace":
                replacement = prepare_selected_replacement(
                    manifest=payload.manifest,
                    parts=tuple(parts),
                    read_snapshot=lambda kind, key, at: package_replacement.read(
                        identity.actor, kind, key, at
                    ),
                    prepare_end=lambda old: package_replacement.end(identity.actor, old),
                )
            parsed = FullScopePackageBuilder().build(
                manifest=payload.manifest,
                parts=tuple(parts),
                replacement_targets=replacement.targets if replacement else (),
                generated_changes=replacement.generated_changes if replacement else (),
            )
            require_package(identity, parsed.change_set)
            maintenance_preview = current_package_preview(identity, parsed.change_set, parts)
            if replacement is not None:
                replacement.revalidate()
            preflight = change_service.preflight(
                school_id=identity.actor.school_id,
                change_set=parsed.change_set,
                context=_read_operation_context(identity, request_context, "preflight"),
                session_binding=identity.session_binding,
            )
            require_package(identity, parsed.change_set)
            record = plan_store.put(
                owner=owner,
                change_set=parsed.change_set,
                package_request_json=(
                    payload.model_dump_json()
                    if any(p.domain_review is not None for p in parts)
                    else None
                ),
                provenance=SchoolFactChangeProvenance(
                    channel="package",
                    raw_source_sha256=parsed.raw_source_sha256,
                    template_version=parsed.input_version,
                    mapping_version="f7_v1",
                    mapping_config_sha256=sha256(b"school_facts_v5:package_builder_v1").hexdigest(),
                    source_size_bytes=sum(item.size_bytes for item in parsed.source_manifest),
                    worksheet_names=(),
                    source_manifest=parsed.source_manifest,
                    row_lineage=parsed.row_lineage,
                    policy_attachments=tuple(
                        SchoolFactPolicyTextAttachment(
                            source_slot=item.source_slot,
                            source_sha256=item.source_sha256,
                            fact_source_key=item.fact_source_key,
                            field_code=item.field_code,
                        )
                        for item in parsed.attachment_bindings
                    ),
                    replacement_targets=parsed.replacement_targets,
                ),
                preflight=preflight,
                source_refs=source_refs,
            )
            if replacement is not None:
                replacement.revalidate()
            maintenance_preview.revalidate()
            if not preflight.is_clean and maintenance_preview.status == "complete":
                maintenance_preview = PackageMaintenancePreview(
                    "incomplete",
                    ("PACKAGE_PREFLIGHT_REJECTED",),
                    maintenance_preview.changes,
                    maintenance_preview.revalidate,
                )
        except Exception as exc:
            _raise_channel_error(exc)
        return _plan_response(
            record,
            replacement_preview=replacement.preview if replacement else (),
            maintenance_preview=maintenance_preview,
        )

    selected_route = (
        router.post("/plans/selected", response_model=SchoolFactPlanResponse)
        if enable_package
        else lambda endpoint: endpoint
    )

    @selected_route
    def preflight_selected(
        payload: SchoolFactSelectedPreflightRequest,
        response: Response,
        identity: Annotated[SchoolFactRequestIdentity, Depends(identity_dependency)],
        request_context: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> SchoolFactPlanResponse:
        _no_store(response)
        require_authority(identity)
        _require_normal_scope(identity)
        created = []
        try:
            if package_source_adapter is None:
                raise FullScopeInputRejected("SOURCE_DOMAIN_ADAPTER_UNAVAILABLE")
            declarations, parts = [], []
            # One explicit time for this authored batch; retries do not move the
            # time because confirmation replays these private normalized sources.
            effective_at = now()
            for index, item in enumerate(payload.items, 1):
                declared, part = package_source_adapter.prepare_form(
                    actor=identity.actor,
                    family=item.family,
                    slot=f"input_{index}",
                    request=item.request,
                    effective_at=effective_at,
                )
                declarations.append(declared)
                parts.append(part)
            versions = {part.domain_review.base_version for part in parts}
            if len(versions) != 1:
                raise FullScopeInputRejected("SOURCE_DOMAIN_SNAPSHOT_CHANGED")
            manifest = FullScopeInputManifestV5(
                input_version="school_facts_v5",
                school_id=identity.actor.school_id,
                base_version=versions.pop(),
                sources=tuple(declarations),
            )
            refs = []
            for source, part in zip(declarations, parts, strict=True):
                stored = source_store.put(
                    filename_summary=f"{source.logical_source_code}.json",
                    content=part.original_bytes,
                    owner=_owner(identity),
                )
                created.append(stored.opaque_ref)
                refs.append(
                    {"source_slot": source.logical_source_code, "source_ref": stored.opaque_ref}
                )
            normalized = SchoolFactPackagePreflightRequest.model_validate_json(
                json.dumps({"manifest": manifest.model_dump(mode="json"), "sources": refs})
            )
            return preflight_package(normalized, response, identity, request_context)
        except Exception as exc:
            for reference in created:
                try:
                    source_store.delete(reference, owner=_owner(identity))
                except Exception:
                    # The session-bound source janitor still expires the bytes.
                    pass
            _raise_channel_error(exc)

    @router.post(
        "/plans/{plan_ref}/confirmation",
        response_model=SchoolFactConfirmationResponse,
    )
    def prepare_confirmation(  # pyright: ignore[reportUnusedFunction]
        plan_ref: OpaqueRef,
        response: Response,
        identity: Annotated[SchoolFactRequestIdentity, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> SchoolFactConfirmationResponse:
        _no_store(response)
        require_authority(identity)
        _require_empty(presence)
        try:
            record = plan_store.get(plan_ref, owner=_owner(identity))
            _require_plan_scope(
                identity,
                change_set=record.change_set,
                channel=record.provenance.channel,
            )
            require_package(identity, record.change_set, record.provenance.channel)
            require_selection(identity, record)
            review = require_complete_preview(identity, record)
            receipt = change_service.prepare_confirmation(
                change_set=record.change_set,
                context=compose_command_context(
                    identity.actor,
                    presence.context.request_id,
                    _idempotency_key(idempotency_key),
                ),
                session_binding=identity.session_binding,
                provenance=record.provenance,
            )
            require_package(identity, record.change_set, record.provenance.channel)
            require_selection(identity, record)
            if review is not None:
                review.revalidate()
        except Exception as exc:
            _raise_channel_error(exc)
        return SchoolFactConfirmationResponse(
            plan_ref=record.plan_ref,
            plan_sha256=receipt.plan_sha256,
            credential=receipt.credential,
            expires_at=receipt.expires_at,
        )

    @router.post("/plans/{plan_ref}/confirm", response_model=SchoolFactApplyResponse)
    def confirm(  # pyright: ignore[reportUnusedFunction]
        plan_ref: OpaqueRef,
        payload: SchoolFactConfirmRequest,
        response: Response,
        identity: Annotated[SchoolFactRequestIdentity, Depends(identity_dependency)],
        request_context: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> SchoolFactApplyResponse:
        _no_store(response)
        require_authority(identity)
        owner = _owner(identity)
        try:
            record = plan_store.get(plan_ref, owner=owner)
            _require_plan_scope(
                identity,
                change_set=record.change_set,
                channel=record.provenance.channel,
            )
            require_package(identity, record.change_set, record.provenance.channel)
            require_selection(identity, record)
            require_complete_preview(identity, record)
            result = change_service.confirm(
                credential=payload.credential,
                change_set=record.change_set,
                context=compose_command_context(
                    identity.actor,
                    request_context.request_id,
                    _idempotency_key(idempotency_key),
                ),
                session_binding=identity.session_binding,
            )
        except Exception as exc:
            _raise_channel_error(exc)
        cleanup_pending = False
        for source_ref in record.source_refs:
            try:
                source_store.delete(source_ref, owner=owner)
            except Exception:
                cleanup_pending = True
        try:
            plan_store.retire(plan_ref, owner=owner)
        except Exception:
            cleanup_pending = True
        return SchoolFactApplyResponse(
            plan_ref=record.plan_ref,
            batch_id=str(result.batch_id),
            target_version=result.target_version,
            changed_count=result.changed_count,
            replayed=result.replayed,
            cleanup_pending=cleanup_pending,
        )

    @router.get("/plans/{plan_ref}/result", response_model=SchoolFactResultResponse)
    def result(  # pyright: ignore[reportUnusedFunction]
        plan_ref: OpaqueRef,
        response: Response,
        identity: Annotated[SchoolFactRequestIdentity, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> SchoolFactResultResponse:
        _no_store(response)
        require_authority(identity)
        _require_empty(presence)
        try:
            result_recheck = authorize_package_result(identity) if package_only else None
            try:
                locator = plan_store.locate(plan_ref, owner=_owner(identity))
            except KeyError:
                if identity.scope != "normal":
                    raise
                restored = change_service.check_result_by_locator(
                    school_id=identity.actor.school_id,
                    actor_user_id=identity.actor.account_user_id,
                    session_binding=identity.session_binding,
                    locator_sha256=sha256(plan_ref.encode("ascii")).hexdigest(),
                    context=_read_operation_context(identity, presence.context, "result"),
                )
                if not restored.found:
                    raise KeyError("PLAN_NOT_FOUND") from None
                authoritative = restored.result
            else:
                if (
                    identity.scope == "school_reactivation_only"
                    and not locator.is_single_school_reactivation
                ):
                    _raise_scope_forbidden()
                authoritative = change_service.check_result_by_digest(
                    school_id=identity.actor.school_id,
                    source_sha256=locator.source_sha256,
                    plan_sha256=locator.plan_sha256,
                    context=_read_operation_context(identity, presence.context, "result"),
                    session_binding=identity.session_binding,
                )
        except Exception as exc:
            _raise_channel_error(exc)
        if result_recheck is not None:
            try:
                result_recheck()
            except Exception as exc:
                _raise_channel_error(exc)
        return SchoolFactResultResponse(
            plan_ref=plan_ref,
            state="pending" if authoritative is None else "applied",
            batch_id=None if authoritative is None else str(authoritative.batch_id),
            target_version=None if authoritative is None else authoritative.target_version,
            changed_count=None if authoritative is None else authoritative.changed_count,
        )

    if result_only:
        result_path = router.prefix + "/plans/{plan_ref}/result"
        retained = [
            route for route in router.routes
            if getattr(route, "path", None) == result_path
            and getattr(route, "methods", None) == {"GET"}
        ]
        if len(retained) != 1:
            raise ValueError("Original package result route is unavailable.")
        router.routes[:] = retained
    elif package_only:
        excluded = {"/plans/excel", "/plans/admin", "/plans/adapted"}
        router.routes[:] = [
            r
            for r in router.routes
            if getattr(r, "path", "").removeprefix(router.prefix) not in excluded
        ]
    return router


__all__ = ["SchoolFactRequestIdentity", "create_school_fact_router"]
