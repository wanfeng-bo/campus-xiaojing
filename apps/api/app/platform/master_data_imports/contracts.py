from __future__ import annotations

from datetime import date, time
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from app.platform.contracts import (
    DisplayName,
    FrozenContractModel,
    PositiveVersion,
    TimezoneName,
)

type ImportMode = Literal[
    "manual_unit",
    "ordinary_atomic_units",
    "authoritative_set_publish",
]
type ImportBatchStatus = Literal[
    "source_registered",
    "mapped",
    "preflight_failed",
    "ready_for_confirmation",
    "applying",
    "partially_failed",
    "applied",
    "published",
]

ImportSha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
OpaqueImportSourceRef = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
SafeFilenameSummary = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]
WorksheetName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
type CanonicalPersonImportField = Literal[
    "person.display_name",
    "person.external_reference",
    "person.valid_from",
]
ExternalReference = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
]
ImportSourceReference = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
type CalendarDayKind = Literal["working", "rest"]
type ObservedCellType = Literal["text", "number", "date", "boolean"]
CalendarNote = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
ConfirmationDecisionReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class ProtectedImportSourceRef(FrozenContractModel):
    """只引用受保护来源及不可变摘要，不携带文件字节或权限事实。"""

    opaque_ref: OpaqueImportSourceRef
    source_sha256: ImportSha256
    filename_summary: SafeFilenameSummary
    size_bytes: int = Field(strict=True, ge=1, le=10 * 1024 * 1024)
    worksheet_names: tuple[WorksheetName, ...] = Field(min_length=1, max_length=32)

    @field_validator("filename_summary")
    @classmethod
    def reject_path_like_filename_summary(cls, value: str) -> str:
        """拒绝路径和控制字符，避免把本机目录或伪路径写入草稿。"""

        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("来源文件名摘要不能包含路径。")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("来源文件名摘要不能包含控制字符。")
        return value


class ColumnMappingEntry(FrozenContractModel):
    """把一个明确表格列映射到一个首批人员导入字段。"""

    source_column_index: int = Field(strict=True, ge=1, le=100)
    source_header: WorksheetName
    target_field_code: CanonicalPersonImportField


class ColumnMappingDraft(FrozenContractModel):
    """保存有版本、无重复来源列和目标字段的人工映射草稿。"""

    mapping_version: PositiveVersion
    entries: tuple[ColumnMappingEntry, ...] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def require_unique_complete_person_mapping(self) -> ColumnMappingDraft:
        """首批人员导入必须明确姓名和外部编号，日期列保持可选。"""

        source_columns = tuple(entry.source_column_index for entry in self.entries)
        target_fields = tuple(entry.target_field_code for entry in self.entries)
        if len(set(source_columns)) != len(source_columns):
            raise ValueError("字段映射不能重复使用同一来源列。")
        if len(set(target_fields)) != len(target_fields):
            raise ValueError("字段映射不能把多个来源列映射到同一目标字段。")
        required = {"person.display_name", "person.external_reference"}
        if not required.issubset(target_fields):
            raise ValueError("人员导入必须明确映射姓名和外部编号。")
        return self


class PersonImportUnitDraft(FrozenContractModel):
    """表达一个不可拆分的自然人及当时姓名候选单元。"""

    row_number: int = Field(strict=True, ge=1, le=10_000)
    display_name: DisplayName
    external_reference: ExternalReference
    valid_from: AwareDatetime


class CalendarDayDraft(FrozenContractModel):
    """表达权威校历候选版本内一个明确自然日。"""

    local_date: date
    day_kind: CalendarDayKind
    actual_start_time: time | None
    actual_end_time: time | None
    note: CalendarNote | None

    @model_validator(mode="after")
    def require_valid_actual_time_pair(self) -> CalendarDayDraft:
        """休息日不携带工作时段，工作时段必须成对且结束晚于开始。"""

        if self.day_kind == "rest":
            if self.actual_start_time is not None or self.actual_end_time is not None:
                raise ValueError("休息日不能携带工作时段。")
            return self
        if (self.actual_start_time is None) != (self.actual_end_time is None):
            raise ValueError("工作时段的开始和结束必须同时提供。")
        if (
            self.actual_start_time is not None
            and self.actual_end_time is not None
            and self.actual_end_time <= self.actual_start_time
        ):
            raise ValueError("工作结束时间必须晚于开始时间。")
        return self


class AuthoritativeCalendarDraft(FrozenContractModel):
    """表达一次确认后整体发布的完整学校日历候选集合。"""

    display_name: DisplayName
    timezone_name: TimezoneName
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None
    days: tuple[CalendarDayDraft, ...] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def require_unique_days_and_non_empty_interval(self) -> AuthoritativeCalendarDraft:
        """候选集合内日期不能重复，发布有效期必须是非空半开区间。"""

        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("校历发布结束时间必须晚于开始时间。")
        local_dates = tuple(day.local_date for day in self.days)
        if len(set(local_dates)) != len(local_dates):
            raise ValueError("同一校历候选版本不能包含重复日期。")
        return self


class StageManualPersonUnitCommand(FrozenContractModel):
    """登记一项人工人员草稿；不携带学校、操作者或确认结果。"""

    mode: Literal["manual_unit"] = "manual_unit"
    source_reference: ImportSourceReference
    unit: PersonImportUnitDraft


class StageOrdinaryPersonImportCommand(FrozenContractModel):
    """登记已由受限 Inspector 和人工映射得到的人员原子单元。"""

    mode: Literal["ordinary_atomic_units"] = "ordinary_atomic_units"
    source: ProtectedImportSourceRef
    worksheet_name: WorksheetName
    mapping: ColumnMappingDraft
    units: tuple[PersonImportUnitDraft, ...] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def require_unique_rows_and_external_references(self) -> StageOrdinaryPersonImportCommand:
        """绑定受保护工作表，并在完整预检前拒绝批内重复标识。"""

        if self.worksheet_name not in self.source.worksheet_names:
            raise ValueError("普通导入选择的工作表不属于受保护来源。")

        row_numbers = tuple(unit.row_number for unit in self.units)
        external_references = tuple(unit.external_reference for unit in self.units)
        if len(set(row_numbers)) != len(row_numbers):
            raise ValueError("人员导入不能包含重复行号。")
        if len(set(external_references)) != len(external_references):
            raise ValueError("人员导入不能包含重复外部编号。")
        return self


class StageAuthoritativeCalendarImportCommand(FrozenContractModel):
    """登记整体发布型校历草稿；只有后续人工 publish 才产生正式事实。"""

    mode: Literal["authoritative_set_publish"] = "authoritative_set_publish"
    source: ProtectedImportSourceRef
    draft: AuthoritativeCalendarDraft


class ImportUnitDecision(FrozenContractModel):
    """绑定预览中的原子单元版本；软异常继续处理时必须留下人工理由。"""

    sequence: int = Field(strict=True, ge=1, le=10_000)
    expected_unit_version: PositiveVersion
    continue_reason: ConfirmationDecisionReason | None = None


class ConfirmImportBatchCommand(FrozenContractModel):
    """确认一张固定预览卡，不携带学校、操作者或权限布尔值。"""

    batch_id: UUID
    expected_batch_version: PositiveVersion
    confirmation_card_sha256: ImportSha256
    decisions: tuple[ImportUnitDecision, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def require_unique_decisions(self) -> ConfirmImportBatchCommand:
        sequences = tuple(decision.sequence for decision in self.decisions)
        if len(set(sequences)) != len(sequences):
            raise ValueError("同一导入单元只能作出一次人工决定。")
        return self


class TakeoverImportUnitDecision(FrozenContractModel):
    """绑定一个待接管单元的稳定序号、版本和可选继续理由。"""

    sequence: int = Field(strict=True, ge=1, le=10_000)
    expected_unit_version: PositiveVersion
    continue_reason: ConfirmationDecisionReason | None = None


class TakeOverOrdinaryImportCommand(FrozenContractModel):
    """显式接管一个普通导入批次，不接收任何权威上下文事实。"""

    batch_id: UUID
    expected_batch_version: PositiveVersion
    confirmation_card_sha256: ImportSha256
    units: tuple[TakeoverImportUnitDecision, ...] = Field(min_length=1, max_length=500)
    takeover_reason: ConfirmationDecisionReason

    @model_validator(mode="after")
    def require_unique_units(self) -> TakeOverOrdinaryImportCommand:
        """拒绝对同一未完成单元重复表达接管决定。"""

        sequences = tuple(unit.sequence for unit in self.units)
        if len(set(sequences)) != len(sequences):
            raise ValueError("同一未完成导入单元只能选择一次。")
        return self


class PreflightImportBatchCommand(FrozenContractModel):
    """把预检绑定到批次和映射版本，禁止沿用已经变化的列映射。"""

    batch_id: UUID
    expected_batch_version: PositiveVersion
    expected_mapping_version: PositiveVersion


class ImportUnitPreview(FrozenContractModel):
    """只暴露单元序号、版本和摘要，不返回人员姓名或源表整行。"""

    sequence: int = Field(strict=True, ge=1, le=10_000)
    row_number: int = Field(strict=True, ge=1, le=10_000)
    unit_version: PositiveVersion
    payload_sha256: ImportSha256
    soft_anomaly_codes: tuple[Literal["same_display_name"], ...] = Field(default=(), max_length=8)


class ImportBatchPreview(FrozenContractModel):
    """人工确认前的固定卡片摘要；正式 owner 写入数恒为零。"""

    batch_id: UUID
    mode: ImportMode
    status: Literal[
        "mapped",
        "preflight_failed",
        "ready_for_confirmation",
        "applying",
        "partially_failed",
        "applied",
        "published",
    ]
    batch_version: PositiveVersion
    confirmation_card_sha256: ImportSha256
    units: tuple[ImportUnitPreview, ...] = Field(min_length=1, max_length=500)
    formal_writes: Literal[0] = 0


class ImportApplyOutcome(FrozenContractModel):
    """返回稳定批次和单元序号，不回显敏感候选内容。"""

    batch_id: UUID
    status: Literal["partially_failed", "applied", "published"]
    batch_version: PositiveVersion
    succeeded_units: tuple[int, ...] = Field(default=(), max_length=500)
    failed_units: tuple[int, ...] = Field(default=(), max_length=500)
    executed_units: tuple[int, ...] = Field(default=(), max_length=500)


class WorksheetInspection(FrozenContractModel):
    """只暴露工作表结构、表头和样本类型，不返回样本原文。"""

    name: WorksheetName
    # SF3 正式事实页允许 10,000 条数据；结构摘要还必须容纳首行表头。
    row_count: int = Field(strict=True, ge=1, le=10_001)
    column_count: int = Field(strict=True, ge=1, le=100)
    headers: tuple[WorksheetName, ...] = Field(min_length=1, max_length=100)
    sample_value_types: tuple[tuple[ObservedCellType, ...], ...] = Field(
        min_length=1,
        max_length=100,
    )

    @model_validator(mode="after")
    def require_one_summary_per_column(self) -> WorksheetInspection:
        """表头、列数和类型摘要必须逐列对齐。"""

        if len(self.headers) != self.column_count:
            raise ValueError("工作表列数必须和表头数量一致。")
        if len(self.sample_value_types) != self.column_count:
            raise ValueError("每一列必须且只能有一项样本类型摘要。")
        return self


class WorkbookInspection(FrozenContractModel):
    """表达未映射 Excel 的受限结构检查结果，正式写入恒为零。"""

    status: Literal["source_registered"] = "source_registered"
    source: ProtectedImportSourceRef
    worksheets: tuple[WorksheetInspection, ...] = Field(min_length=1, max_length=32)
    proposed_mapping: None = None
    formal_writes: Literal[0] = 0
