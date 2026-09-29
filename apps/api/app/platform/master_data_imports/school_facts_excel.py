from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from typing import BinaryIO, Literal, Protocol, cast

from openpyxl import load_workbook
from pydantic import AwareDatetime, Field, ValidationError

from app.platform.contracts import FrozenContractModel
from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from app.platform.master_data_imports.excel import ImportWorkbookRejected, RestrictedExcelInspector
from app.platform.master_data_imports.status_mappings import (
    DEFAULT_STATUS_MAPPING_PROFILE,
    MAX_SCHOOL_FACT_STATUS_DECISIONS,
    StatusMappingError,
    StatusMappingProfile,
    StatusMappingResult,
    map_school_status,
    status_mapping_profile_sha256,
)
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    CanonicalFactContentV1,
    CanonicalFactContentV4,
    safe_content_payload,
)
from app.platform.school_facts.ports import (
    SchoolFactRowProvenance,
    SchoolFactSourceArtifact,
)

SCHOOL_FACT_TEMPLATE_VERSION = "school_facts_v2"
SCHOOL_FACT_ADMIN_MAPPING_VERSION = "admin_v2"
SCHOOL_FACT_ADMIN_MAPPING_CONFIG_SHA256 = sha256(
    b"school_facts:admin_v2:canonical-fields"
).hexdigest()
_CONTROL_HEADERS = ("操作", "稳定标识", "预期版本", "生效时间")
_COMMON_OPERATIONS = ("upsert", "end", "revoke")
_ROOT_FACT_KINDS = frozenset({"school", "person", "student", "organization", "location"})


class _ValueWorksheet(Protocol):
    def iter_rows(self, *, values_only: bool) -> Iterator[tuple[object, ...]]: ...


class SchoolFactWorkbookIssue(ImportWorkbookRejected):
    """只携带服务端登记上下文和安全坐标的预检问题。"""

    def __init__(
        self,
        *,
        code: str,
        sheet_code: str,
        row_number: int,
        field_code: str,
        mapping_version: str,
        source_slot: str | None = None,
    ) -> None:
        self.code = code
        self.sheet_code = sheet_code
        self.row_number = row_number
        self.field_code = field_code
        self.mapping_version = mapping_version
        self.source_slot = source_slot
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SchoolFactSheetSpec:
    origin_code: str
    fact_kind: str | None
    headers: tuple[str, ...]
    content_headers: tuple[tuple[str, str], ...] = ()
    allowed_operations: tuple[str, ...] = ()


def _data_spec(
    origin_code: str,
    fact_kind: str,
    *content_headers: tuple[str, str],
) -> SchoolFactSheetSpec:
    return SchoolFactSheetSpec(
        origin_code=origin_code,
        fact_kind=fact_kind,
        headers=(*_CONTROL_HEADERS, *(header for header, _field in content_headers)),
        content_headers=content_headers,
        allowed_operations=(
            (*_COMMON_OPERATIONS, "reactivate")
            if fact_kind in _ROOT_FACT_KINDS
            else _COMMON_OPERATIONS
        ),
    )


SCHOOL_FACT_SHEET_SPECS: dict[str, SchoolFactSheetSpec] = {
    "00_导入说明": SchoolFactSheetSpec("instructions", None, ("模板版本", "说明")),
    "01_学校": _data_spec(
        "schools",
        "school",
        ("学校编号", "business_code"),
        ("学校名称", "display_name"),
        ("学校简称", "short_name"),
        ("时区", "timezone_name"),
        ("状态", "lifecycle_status"),
    ),
    "02_人员": _data_spec(
        "people",
        "person",
        ("人员编号", "business_code"),
        ("姓名", "display_name"),
        ("人员类型", "person_kind"),
        ("状态", "lifecycle_status"),
    ),
    "03_学生": _data_spec(
        "students",
        "student",
        ("人员稳定标识", "person_source_key"),
        ("学生状态", "status"),
    ),
    "04_学生在校状态": _data_spec(
        "student_school_relationships",
        "student_school_relationship",
        ("学生稳定标识", "student_source_key"),
        ("学号", "student_number"),
        ("在校状态", "enrollment_status"),
        ("居住方式", "residence_mode"),
        ("主要校区稳定标识", "primary_campus_source_key"),
        ("行政校区稳定标识", "administrative_campus_source_key"),
    ),
    "05_登录映射": _data_spec(
        "login_mappings",
        "login_mapping",
        ("登录提供方", "provider_code"),
        ("租户标识", "provider_tenant_key"),
        ("登录主体", "external_subject"),
        ("人员稳定标识", "person_source_key"),
        ("状态", "status"),
    ),
    "06_组织": _data_spec(
        "organizations",
        "organization",
        ("上级组织稳定标识", "parent_source_key"),
        ("组织类型", "kind"),
        ("组织编号", "business_code"),
        ("组织名称", "display_name"),
        ("组织简称", "short_name"),
        ("展示路径", "display_path"),
        ("状态", "status"),
    ),
    "07_任职": _data_spec(
        "assignments",
        "assignment",
        ("人员稳定标识", "person_source_key"),
        ("组织稳定标识", "organization_source_key"),
        ("岗位名称", "position_title"),
        ("语义角色编码", "semantic_role_code"),
        ("任职类型", "assignment_kind"),
        ("任命来源", "appointment_source"),
        ("批准记录", "approval_reference"),
        ("状态", "status"),
        ("计划结束时间", "valid_to"),
    ),
    "08_地点": _data_spec(
        "locations",
        "location",
        ("上级地点稳定标识", "parent_source_key"),
        ("地点类型", "kind"),
        ("地点编号", "business_code"),
        ("地点名称", "display_name"),
        ("展示路径", "display_path"),
        ("其他名称", "aliases"),
        ("状态", "status"),
    ),
    "09_地点别名": _data_spec(
        "location_aliases",
        "location_alias",
        ("地点稳定标识", "location_source_key"),
        ("别名", "alias"),
        ("别名类型", "alias_kind"),
    ),
    "10_宿舍房间属性": _data_spec(
        "dorm_rooms",
        "dorm_room",
        ("房间地点稳定标识", "room_source_key"),
        ("核定床位数", "authorized_bed_capacity"),
        ("住宿区域", "accommodation_zone"),
        ("是否可用", "is_available"),
        ("无障碍标签", "accessibility_tags"),
    ),
    "11_宿舍床位": _data_spec(
        "dorm_beds",
        "dorm_bed",
        ("房间地点稳定标识", "room_source_key"),
        ("床位编号", "business_code"),
        ("床位名称", "display_name"),
        ("可用状态", "availability"),
    ),
    "12_学生入住": _data_spec(
        "student_bed_residences",
        "student_bed_residence",
        ("学生稳定标识", "student_source_key"),
        ("床位稳定标识", "bed_source_key"),
        ("结束时间", "valid_to"),
    ),
    "13_个人联系方式": _data_spec(
        "person_contacts",
        "person_contact",
        ("人员稳定标识", "person_source_key"),
        ("联系方式类型", "contact_kind"),
        ("联系内容", "contact_value"),
        ("主要程度", "is_primary"),
        ("可见范围", "visibility_scope"),
        ("受众编码", "audience_code"),
        ("同意记录", "consent_reference"),
    ),
    "14_学生联系人": _data_spec(
        "student_contacts",
        "student_contact",
        ("学生稳定标识", "student_source_key"),
        ("联系人稳定标识", "contact_person_source_key"),
        ("关系", "relationship_kind"),
        ("联系顺序", "priority"),
        ("是否紧急联系人", "is_emergency"),
    ),
    "15_官方联系方式": _data_spec(
        "official_contacts",
        "official_contact",
        ("所有者类型", "owner_kind"),
        ("组织稳定标识", "organization_source_key"),
        ("地点稳定标识", "location_source_key"),
        ("联系方式类型", "contact_kind"),
        ("联系内容", "contact_value"),
        ("服务时间", "service_hours"),
        ("状态", "lifecycle_status"),
        ("可见范围", "visibility_scope"),
        ("受众编码", "audience_code"),
    ),
    "16_制度目录": _data_spec("policies", "policy"),
    "17_制度版本": _data_spec(
        "policy_versions",
        "policy_version",
        ("制度目录稳定标识", "policy_source_key"),
        ("制度编号", "business_code"),
        ("制度名称", "display_name"),
        ("分类编码", "category_code"),
        ("可见分类", "classification"),
        ("制度状态", "__policy_status__"),
        ("发布组织稳定标识", "publishing_organization_source_key"),
        ("发布时间", "published_at"),
        ("生效开始", "effective_from"),
        ("生效结束", "effective_to"),
        ("制度正文", "content_text"),
        ("版本说明", "version_note"),
        ("替代版本稳定标识", "supersedes_policy_version_source_key"),
    ),
    "18_组织校区关系": _data_spec(
        "organization_campus_relationships",
        "organization_campus_relationship",
        ("组织稳定标识", "organization_source_key"),
        ("校区稳定标识", "campus_source_key"),
        ("结束时间", "valid_to"),
    ),
    "19_制度适用范围": _data_spec(
        "policy_scopes",
        "policy_scope",
        ("制度目录稳定标识", "policy_source_key"),
        ("组织稳定标识", "organization_source_key"),
        ("校区稳定标识", "campus_source_key"),
        ("结束时间", "valid_to"),
    ),
}

_STATUS_FIELDS: dict[tuple[str, str], str] = {
    ("01_学校", "lifecycle_status"): "lifecycle_status",
    ("02_人员", "lifecycle_status"): "lifecycle_status",
    ("03_学生", "status"): "status",
    ("04_学生在校状态", "enrollment_status"): "enrollment_status",
    ("04_学生在校状态", "residence_mode"): "residence_mode",
    ("05_登录映射", "status"): "status",
    ("06_组织", "status"): "status",
    ("07_任职", "assignment_kind"): "assignment_kind",
    ("07_任职", "status"): "status",
    ("08_地点", "status"): "status",
    ("11_宿舍床位", "availability"): "availability",
    ("15_官方联系方式", "lifecycle_status"): "lifecycle_status",
    ("17_制度版本", "classification"): "classification",
    ("17_制度版本", "__policy_status__"): "policy_status",
}
_DATETIME_FIELDS = {"published_at", "effective_from", "effective_to", "valid_to"}
_INTEGER_FIELDS = {"authorized_bed_capacity", "priority"}
_BOOLEAN_FIELDS = {"is_available", "is_emergency"}
_TUPLE_FIELDS = {"aliases", "accessibility_tags"}

F6_SCHOOL_FACT_SHEET_SPECS = {
    **SCHOOL_FACT_SHEET_SPECS,
    "22_宿舍用途": _data_spec(
        "dormitory_building_profiles",
        "dormitory_building_profile",
        ("地点稳定标识", "location_source_key"),
        ("用途类别", "usage_kind"),
        ("名称", "display_name"),
        ("说明", "description"),
        ("是否可用", "is_available"),
        ("结束时间", "valid_to"),
    ),
}


@dataclass(frozen=True, slots=True)
class SchoolFactWorkbookPlan:
    template_version: Literal["school_facts_v2", "school_facts_v4"]
    raw_source_sha256: str
    mapping_version: str
    mapping_config_sha256: str
    standard_artifact_size_bytes: int
    change_set: CanonicalFactChangeSetV1
    status_decisions: tuple[StatusMappingResult, ...]
    source_manifest: tuple[SchoolFactSourceArtifact, ...]
    row_lineage: tuple[SchoolFactRowProvenance, ...]

    def __post_init__(self) -> None:
        if len(self.status_decisions) > MAX_SCHOOL_FACT_STATUS_DECISIONS:
            raise ValueError("学校事实状态映射决定超过机械容量边界。")


class SchoolFactAdminChangeV1(FrozenContractModel):
    """管理台只提交变更内容；来源坐标由服务端按固定表序号生成。"""

    schema_version: Literal[1, 4] = 1
    operation: Literal["upsert", "end", "revoke", "reactivate"]
    fact_kind: str
    source_key: str
    expected_version: int
    effective_at: AwareDatetime = Field(strict=False)
    content: CanonicalFactContentV4 | CanonicalFactContentV1 | None


def _canonical_source_sha256(changes: tuple[CanonicalFactChangeV1, ...]) -> str:
    payload = [change.model_dump(mode="json", exclude_none=True) for change in changes]
    for change, item in zip(changes, payload, strict=True):
        if change.schema_version == 4:
            item["content"] = safe_content_payload(change.content)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return sha256(encoded).hexdigest()


class SchoolFactAdminPlanBuilder:
    """管理台只把已类型化的小批变更收敛为与 Excel 相同的 SF2 契约。"""

    def build(
        self,
        *,
        base_version: int,
        changes: tuple[CanonicalFactChangeV1, ...],
    ) -> CanonicalFactChangeSetV1:
        return CanonicalFactChangeSetV1(
            schema_version=4 if any(c.schema_version == 4 for c in changes) else 1,
            source_sha256=_canonical_source_sha256(changes),
            base_version=base_version,
            changes=changes,
        )

    def build_from_inputs(
        self,
        *,
        base_version: int,
        inputs: tuple[SchoolFactAdminChangeV1, ...],
    ) -> CanonicalFactChangeSetV1:
        registered_specs = tuple(
            spec for spec in F6_SCHOOL_FACT_SHEET_SPECS.values() if spec.fact_kind is not None
        )
        origin_by_kind = {spec.fact_kind: spec.origin_code for spec in registered_specs}
        order_by_kind = {spec.fact_kind: order for order, spec in enumerate(registered_specs)}
        for item in inputs:
            if item.fact_kind not in origin_by_kind:
                raise ImportWorkbookRejected("管理台事实类别未登记。")
        ordered_inputs = tuple(
            item
            for _original_order, item in sorted(
                enumerate(inputs),
                key=lambda pair: (order_by_kind[pair[1].fact_kind], pair[0]),
            )
        )
        changes: list[CanonicalFactChangeV1] = []
        row_count_by_kind: dict[str, int] = {}
        schema_version = 4 if any(item.schema_version == 4 for item in inputs) else 1
        for item in ordered_inputs:
            row_count_by_kind[item.fact_kind] = row_count_by_kind.get(item.fact_kind, 0) + 1
            if row_count_by_kind[item.fact_kind] > 10_000:
                raise ImportWorkbookRejected("管理台同类事实一次不能超过 10000 条。")
            row_number = row_count_by_kind[item.fact_kind] + 1
            origin_code = origin_by_kind[item.fact_kind]
            try:
                changes.append(
                    CanonicalFactChangeV1.model_validate(
                        {
                            **item.model_dump(mode="python", exclude_none=True, exclude_unset=True),
                            "schema_version": schema_version,
                            "origin": {"sheet_code": origin_code, "row_number": row_number},
                        }
                    )
                )
            except ValidationError:
                raise ImportWorkbookRejected("管理台变更不符合规范事实契约。") from None
        try:
            return self.build(base_version=base_version, changes=tuple(changes))
        except ValidationError:
            raise ImportWorkbookRejected("管理台变更集不符合规范事实契约。") from None


class SchoolFactWorkbookParser:
    """正式工作簿只产生规范变更，不拥有任何事实写端口。"""

    def __init__(self, *, inspector: RestrictedExcelInspector | None = None) -> None:
        # 物理行上限含一行表头；业务数据行仍在 parse 中机械锁为 10,000。
        self._inspector = inspector or RestrictedExcelInspector(max_rows=10_001)

    def parse(
        self,
        stream: BinaryIO,
        *,
        source: ProtectedImportSourceRef,
        base_version: int,
        mapping_profile: StatusMappingProfile = DEFAULT_STATUS_MAPPING_PROFILE,
    ) -> SchoolFactWorkbookPlan:
        content = stream.read(10 * 1024 * 1024 + 1)
        inspection = self._inspector.inspect(BytesIO(content), source=source)
        instruction_book = load_workbook(
            BytesIO(content), read_only=True, data_only=False, keep_links=False
        )
        try:
            if "00_导入说明" not in instruction_book.sheetnames:
                raise ImportWorkbookRejected("缺少正式模板说明。")
            template_version = self._validate_instruction(instruction_book["00_导入说明"])
        finally:
            instruction_book.close()
        specs = (
            F6_SCHOOL_FACT_SHEET_SPECS
            if template_version == "school_facts_v4"
            else SCHOOL_FACT_SHEET_SPECS
        )
        expected_names = frozenset(specs)
        actual_names = frozenset(item.name for item in inspection.worksheets)
        if actual_names != expected_names or len(inspection.worksheets) != len(expected_names):
            raise ImportWorkbookRejected("Excel 工作表集合必须与 school_facts_v2 完全一致。")
        total_data_rows = 0
        for item in inspection.worksheets:
            spec = specs[item.name]
            if len(set(item.headers)) != len(item.headers) or frozenset(item.headers) != frozenset(
                spec.headers
            ):
                raise ImportWorkbookRejected("Excel 工作表表头必须与正式模板精确一致。")
            if spec.fact_kind is not None:
                data_rows = item.row_count - 1
                if data_rows > 10_000:
                    raise ImportWorkbookRejected("Excel 每个数据页不能超过 10000 行。")
                total_data_rows += data_rows
        if total_data_rows > 50_000:
            raise ImportWorkbookRejected("Excel 数据总行数不能超过 50000 行。")

        workbook = load_workbook(
            BytesIO(content), read_only=True, data_only=False, keep_links=False
        )
        changes: list[CanonicalFactChangeV1] = []
        decisions: list[StatusMappingResult] = []
        try:
            self._validate_instruction(workbook["00_导入说明"])
            for sheet_name, spec in specs.items():
                if spec.fact_kind is None:
                    continue
                worksheet = workbook[sheet_name]
                rows = worksheet.iter_rows(values_only=True)
                headers = tuple(next(rows))
                for row_number, values in enumerate(rows, start=2):
                    if all(value is None for value in values):
                        continue
                    row = cast(
                        dict[object, object],
                        dict(zip(headers, values, strict=True)),
                    )
                    change, row_decisions = self._parse_row(
                        sheet_name=sheet_name,
                        spec=spec,
                        row_number=row_number,
                        row=row,
                        mapping_profile=mapping_profile,
                        schema_version=4 if template_version == "school_facts_v4" else 1,
                    )
                    changes.append(change)
                    decisions.extend(row_decisions)
        finally:
            workbook.close()
        if not changes:
            raise ImportWorkbookRejected("Excel 没有可预检的学校事实变更。")
        change_tuple = tuple(changes)
        try:
            change_set = SchoolFactAdminPlanBuilder().build(
                base_version=base_version,
                changes=change_tuple,
            )
        except ValidationError:
            raise ImportWorkbookRejected("Excel 规范变更不符合学校事实契约。") from None
        return SchoolFactWorkbookPlan(
            template_version=template_version,
            raw_source_sha256=source.source_sha256,
            mapping_version=mapping_profile.version,
            mapping_config_sha256=self._formal_mapping_config_sha256(
                mapping_profile, template_version
            ),
            standard_artifact_size_bytes=source.size_bytes,
            change_set=change_set,
            status_decisions=tuple(decisions),
            source_manifest=(
                SchoolFactSourceArtifact(
                    logical_source_code="formal_workbook",
                    source_sha256=source.source_sha256,
                    size_bytes=source.size_bytes,
                ),
            ),
            row_lineage=tuple(
                SchoolFactRowProvenance(
                    fact_kind=change.fact_kind,
                    fact_source_key=change.source_key,
                    raw_source_sha256=source.source_sha256,
                    source_slot="formal_workbook",
                    source_sheet_code=change.origin.sheet_code,
                    source_row_number=change.origin.row_number,
                    target_field_codes=(
                        "operation",
                        "source_key",
                        "expected_version",
                        "effective_at",
                        *sorted((safe_content_payload(change.content) or {}).keys()),
                    ),
                    source_field_codes=(
                        "operation",
                        "source_key",
                        "expected_version",
                        "effective_at",
                        *sorted((safe_content_payload(change.content) or {}).keys()),
                    ),
                )
                for change in change_set.changes
            ),
        )

    @staticmethod
    def _formal_mapping_config_sha256(
        profile: StatusMappingProfile, template_version: str = SCHOOL_FACT_TEMPLATE_VERSION
    ) -> str:
        return sha256(
            (f"{template_version}:{status_mapping_profile_sha256(profile)}").encode("ascii")
        ).hexdigest()

    @staticmethod
    def _validate_instruction(worksheet: object) -> Literal["school_facts_v2", "school_facts_v4"]:
        rows = cast(_ValueWorksheet, worksheet).iter_rows(values_only=True)
        next(rows)
        first = next(rows, None)
        if first is None or first[0] not in {SCHOOL_FACT_TEMPLATE_VERSION, "school_facts_v4"}:
            raise ImportWorkbookRejected("Excel 模板版本不是 school_facts_v2。")
        return cast(Literal["school_facts_v2", "school_facts_v4"], first[0])

    def _parse_row(
        self,
        *,
        sheet_name: str,
        spec: SchoolFactSheetSpec,
        row_number: int,
        row: dict[object, object],
        mapping_profile: StatusMappingProfile,
        schema_version: int = 1,
    ) -> tuple[CanonicalFactChangeV1, tuple[StatusMappingResult, ...]]:
        operation = self._text(row.get("操作"))
        if operation not in spec.allowed_operations:
            raise ImportWorkbookRejected("Excel 操作不适用于该事实类别。")
        source_key = self._text(row.get("稳定标识"))
        expected_version = row.get("预期版本")
        if type(expected_version) is not int or expected_version < 0:
            raise ImportWorkbookRejected("Excel 预期版本必须是非负整数。")
        effective_at = self._aware_datetime(row.get("生效时间"))
        content: dict[str, object] | None = None
        decisions: list[StatusMappingResult] = []
        if operation in {"upsert", "reactivate"}:
            content = {}
            for header, field_code in spec.content_headers:
                value = row.get(header)
                if value is None or (isinstance(value, str) and not value.strip()):
                    if (
                        schema_version == 4
                        and spec.fact_kind == "student_school_relationship"
                        and field_code == "residence_mode"
                    ):
                        raise ImportWorkbookRejected(
                            "住校方式必须明确填写；未知使用unknown，不以空白清除。"
                        )
                    continue
                if (
                    schema_version == 4
                    and field_code == "residence_mode"
                    and value in {"unknown", "未知"}
                ):
                    content[field_code] = None
                    continue
                status_field = _STATUS_FIELDS.get((sheet_name, field_code))
                if status_field is not None:
                    try:
                        decision = map_school_status(
                            sheet_code=sheet_name,
                            field_code=status_field,
                            raw_value=value,
                            profile=mapping_profile,
                            row_number=row_number,
                        )
                    except StatusMappingError as exc:
                        raise SchoolFactWorkbookIssue(
                            code=exc.code,
                            sheet_code=sheet_name,
                            row_number=row_number,
                            field_code=status_field,
                            mapping_version=mapping_profile.version,
                        ) from None
                    decisions.append(decision)
                    if not field_code.startswith("__"):
                        content[field_code] = decision.standard_value
                elif field_code == "is_primary":
                    try:
                        decision = map_school_status(
                            sheet_code=sheet_name,
                            field_code="contact_priority",
                            raw_value=value,
                            profile=mapping_profile,
                            row_number=row_number,
                        )
                    except StatusMappingError as exc:
                        raise SchoolFactWorkbookIssue(
                            code=exc.code,
                            sheet_code=sheet_name,
                            row_number=row_number,
                            field_code="contact_priority",
                            mapping_version=mapping_profile.version,
                        ) from None
                    decisions.append(decision)
                    content[field_code] = decision.standard_value == "primary"
                else:
                    content[field_code] = self._content_value(field_code, value)
        elif any(
            value is not None and (not isinstance(value, str) or bool(value.strip()))
            for header, _field_code in spec.content_headers
            if (value := row.get(header)) is not None
        ):
            raise ImportWorkbookRejected("Excel 结束或撤销行不能夹带替换内容。")
        try:
            return (
                CanonicalFactChangeV1.model_validate(
                    {
                        "schema_version": schema_version,
                        "operation": operation,
                        "fact_kind": spec.fact_kind,
                        "source_key": source_key,
                        "expected_version": expected_version,
                        "effective_at": effective_at,
                        "origin": {"sheet_code": spec.origin_code, "row_number": row_number},
                        "content": content,
                    }
                ),
                tuple(decisions),
            )
        except ValidationError:
            raise ImportWorkbookRejected(
                f"Excel {sheet_name} 第 {row_number} 行不符合规范事实契约。"
            ) from None

    @staticmethod
    def _text(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ImportWorkbookRejected("Excel 必填文本字段不能为空。")
        return value.strip()

    @classmethod
    def _content_value(cls, field_code: str, value: object) -> object:
        if field_code in _DATETIME_FIELDS:
            return cls._aware_datetime(value)
        if field_code in _INTEGER_FIELDS:
            if type(value) is not int:
                raise ImportWorkbookRejected("Excel 整数字段格式无效。")
            return value
        if field_code in _BOOLEAN_FIELDS:
            if type(value) is bool:
                return value
            if isinstance(value, str) and value.strip() in {"是", "true"}:
                return True
            if isinstance(value, str) and value.strip() in {"否", "false"}:
                return False
            raise ImportWorkbookRejected("Excel 是否字段格式无效。")
        if field_code in _TUPLE_FIELDS:
            if not isinstance(value, str):
                raise ImportWorkbookRejected("Excel 多值字段必须是逗号分隔文本。")
            return tuple(item.strip() for item in value.split(",") if item.strip())
        if not isinstance(value, str) or not value.strip():
            raise ImportWorkbookRejected("Excel 文本字段格式无效。")
        return value.strip()

    @staticmethod
    def _aware_datetime(value: object) -> datetime:
        candidate: datetime
        if isinstance(value, datetime):
            candidate = value
        elif isinstance(value, str) and 1 <= len(value.strip()) <= 64:
            normalized = value.strip()
            if normalized.endswith(("Z", "z")):
                normalized = f"{normalized[:-1]}+00:00"
            try:
                candidate = datetime.fromisoformat(normalized)
            except ValueError:
                raise ImportWorkbookRejected("Excel 时间必须是含时区的 ISO-8601 文本。") from None
        else:
            raise ImportWorkbookRejected("Excel 时间必须是含时区的 ISO-8601 文本。")
        if candidate.tzinfo is None or candidate.utcoffset() is None:
            raise ImportWorkbookRejected("Excel 时间必须包含明确时区。")
        return candidate


__all__ = [
    "SCHOOL_FACT_SHEET_SPECS",
    "SCHOOL_FACT_ADMIN_MAPPING_CONFIG_SHA256",
    "SCHOOL_FACT_ADMIN_MAPPING_VERSION",
    "SCHOOL_FACT_TEMPLATE_VERSION",
    "SchoolFactAdminPlanBuilder",
    "SchoolFactAdminChangeV1",
    "SchoolFactSheetSpec",
    "SchoolFactWorkbookIssue",
    "SchoolFactWorkbookParser",
    "SchoolFactWorkbookPlan",
]
