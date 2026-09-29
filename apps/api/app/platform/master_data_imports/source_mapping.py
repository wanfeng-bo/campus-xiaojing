from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openpyxl import Workbook, load_workbook

from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from app.platform.master_data_imports.excel import ImportWorkbookRejected, RestrictedExcelInspector
from app.platform.master_data_imports.ports import (
    PrivateImportSourceAccessDenied,
    PrivateImportSourceExpired,
    PrivateImportSourceStore,
    PrivateSourceOwner,
)
from app.platform.master_data_imports.school_facts_excel import (
    F6_SCHOOL_FACT_SHEET_SPECS,
    SCHOOL_FACT_SHEET_SPECS,
    SCHOOL_FACT_TEMPLATE_VERSION,
    SchoolFactWorkbookIssue,
    SchoolFactWorkbookParser,
    SchoolFactWorkbookPlan,
)
from app.platform.master_data_imports.status_mappings import (
    DEFAULT_STATUS_MAPPING_PROFILE,
    StatusMappingProfile,
    StatusMappingResult,
    status_mapping_profile_sha256,
)
from app.platform.school_facts.contracts import require_neutral_lineage_code
from app.platform.school_facts.ports import (
    SchoolFactRowProvenance,
    SchoolFactSourceArtifact,
)


class SourceMappingError(ValueError):
    """来源适配只返回固定代码，不回显学校原表值。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


MAX_ADAPTED_SOURCE_COUNT = 8
MAX_ADAPTED_SOURCE_TOTAL_BYTES = 32 * 1024 * 1024


def require_adapted_source_capacity(sources: tuple[ProtectedImportSourceRef, ...]) -> None:
    """固定来源适配集合的文件数与原始总字节，不依赖具体临时存储实现。"""

    if (
        not 1 <= len(sources) <= MAX_ADAPTED_SOURCE_COUNT
        or sum(source.size_bytes for source in sources) > MAX_ADAPTED_SOURCE_TOTAL_BYTES
    ):
        raise SourceMappingError("SOURCE_SET_CAPACITY_EXCEEDED")


@dataclass(frozen=True, slots=True)
class SourceColumnMapping:
    source_header: str
    target_header: str
    constant: object | None = None
    additional_source_headers: tuple[str, ...] = ()
    join_with: str = ""
    split_delimiter: str | None = None
    split_index: int | None = None
    value_map: tuple[tuple[str, str], ...] = ()
    datetime_format: str | None = None
    timezone_name: str | None = None
    source_field_code: str | None = None

    def __post_init__(self) -> None:
        if not self.source_header or not self.target_header:
            raise ValueError("来源列和目标列不能为空。")
        if self.split_delimiter is None and self.split_index is not None:
            raise ValueError("拆分索引必须同时声明拆分符。")
        if self.split_delimiter is not None and self.split_index is None:
            raise ValueError("拆分符必须同时声明拆分索引。")
        if len({source for source, _target in self.value_map}) != len(self.value_map):
            raise ValueError("来源值转换不能包含重复来源。")
        if (self.datetime_format is None) != (self.timezone_name is None):
            raise ValueError("日期清洗必须同时声明格式和时区。")
        if self.source_field_code is not None:
            require_neutral_lineage_code(self.source_field_code)


@dataclass(frozen=True, slots=True)
class SourceSheetMapping:
    source_ref: str
    source_sheet: str
    target_sheet: str
    columns: tuple[SourceColumnMapping, ...]
    source_slot: str = "source_1"
    source_sheet_code: str = "source_sheet"

    def __post_init__(self) -> None:
        if (
            self.target_sheet == "00_导入说明"
            or self.target_sheet not in F6_SCHOOL_FACT_SHEET_SPECS
        ):
            raise ValueError("来源适配目标必须是正式数据页。")
        targets = tuple(item.target_header for item in self.columns)
        if len(set(targets)) != len(targets):
            raise ValueError("同一来源表不能重复生成同一目标列。")
        if not targets:
            raise ValueError("来源表至少需要一项列映射。")
        require_neutral_lineage_code(self.source_slot)
        require_neutral_lineage_code(self.source_sheet_code)


@dataclass(frozen=True, slots=True)
class SourceMappingProfile:
    version: str
    sheets: tuple[SourceSheetMapping, ...]
    status_profile: StatusMappingProfile = DEFAULT_STATUS_MAPPING_PROFILE
    template_version: str = SCHOOL_FACT_TEMPLATE_VERSION

    def __post_init__(self) -> None:
        if not self.version or not self.sheets:
            raise ValueError("来源映射必须带版本且不能为空。")
        if self.template_version not in {SCHOOL_FACT_TEMPLATE_VERSION, "school_facts_v4"}:
            raise ValueError("来源映射模板版本无效。")
        specs = (
            F6_SCHOOL_FACT_SHEET_SPECS
            if self.template_version == "school_facts_v4"
            else SCHOOL_FACT_SHEET_SPECS
        )
        if any(sheet.target_sheet not in specs for sheet in self.sheets):
            raise ValueError("来源映射数据页不属于所选模板版本。")
        slot_refs: dict[str, str] = {}
        sheet_codes: set[tuple[str, str]] = set()
        for sheet in self.sheets:
            existing_ref = slot_refs.setdefault(sheet.source_slot, sheet.source_ref)
            if existing_ref != sheet.source_ref:
                raise ValueError("同一逻辑来源槽不能绑定多个临时来源。")
            key = (sheet.source_slot, sheet.source_sheet_code)
            if key in sheet_codes:
                raise ValueError("来源映射逻辑表编码重复。")
            sheet_codes.add(key)


@dataclass(frozen=True, slots=True)
class ResolvedSourceMappingProfile:
    """服务端登记配置的不可变解析结果；摘要不能由客户端自报。"""

    school_id: str
    source_slots: tuple[str, ...]
    profile: SourceMappingProfile
    config_sha256: str

    def __post_init__(self) -> None:
        try:
            UUID(self.school_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("来源映射学校绑定无效。") from None
        if self.source_slots != tuple(sorted(set(self.source_slots))):
            raise ValueError("来源映射逻辑槽绑定无效。")
        for source_slot in self.source_slots:
            require_neutral_lineage_code(source_slot)
        registered_slots = tuple(sorted({sheet.source_slot for sheet in self.profile.sheets}))
        if registered_slots != self.source_slots or any(
            sheet.source_ref != sheet.source_slot for sheet in self.profile.sheets
        ):
            raise ValueError("来源映射必须使用登记的中性逻辑槽。")
        if self.config_sha256 != source_mapping_profile_sha256(self.profile):
            raise ValueError("来源映射配置摘要与登记内容不一致。")

    def bind_source_refs(self, bindings: dict[str, str]) -> SourceMappingProfile:
        if tuple(sorted(bindings)) != self.source_slots:
            raise ValueError("来源映射临时来源绑定不完整。")
        return replace(
            self.profile,
            sheets=tuple(
                replace(sheet, source_ref=bindings[sheet.source_slot])
                for sheet in self.profile.sheets
            ),
        )


@dataclass(frozen=True, slots=True)
class _AdaptedRowOrigin:
    raw_source_sha256: str
    source_slot: str
    source_sheet_code: str
    source_row_number: int
    target_field_codes: tuple[str, ...]
    source_field_codes: tuple[str, ...]


def source_mapping_profile_sha256(profile: SourceMappingProfile) -> str:
    """摘要完整确定性配置，但明确排除每次上传才产生的 opaque source_ref。"""

    payload = {
        "version": profile.version,
        "status_profile_sha256": status_mapping_profile_sha256(profile.status_profile),
        "sheets": [
            {
                "source_slot": sheet.source_slot,
                "source_sheet": sheet.source_sheet,
                "source_sheet_code": sheet.source_sheet_code,
                "target_sheet": sheet.target_sheet,
                "columns": [
                    {
                        "source_header": column.source_header,
                        "target_header": column.target_header,
                        "constant": column.constant,
                        "additional_source_headers": column.additional_source_headers,
                        "join_with": column.join_with,
                        "split_delimiter": column.split_delimiter,
                        "split_index": column.split_index,
                        "value_map": sorted(column.value_map),
                        "datetime_format": column.datetime_format,
                        "timezone_name": column.timezone_name,
                        "source_field_code": column.source_field_code,
                    }
                    for column in sorted(sheet.columns, key=lambda item: item.target_header)
                ],
            }
            for sheet in profile.sheets
        ],
    }
    if profile.template_version != SCHOOL_FACT_TEMPLATE_VERSION:
        payload["template_version"] = profile.template_version
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class SchoolFactSourceAdapter:
    """只把学校原表转成正式标准工作簿，不写事实也不确认。"""

    def __init__(self, *, inspector: RestrictedExcelInspector | None = None) -> None:
        self._inspector = inspector or RestrictedExcelInspector(max_rows=10_001)

    def adapt(
        self,
        *,
        sources: dict[str, tuple[ProtectedImportSourceRef, bytes]],
        profile: SourceMappingProfile,
        base_version: int,
    ) -> SchoolFactWorkbookPlan:
        require_adapted_source_capacity(tuple(source for source, _content in sources.values()))
        specs = (
            F6_SCHOOL_FACT_SHEET_SPECS
            if profile.template_version == "school_facts_v4"
            else SCHOOL_FACT_SHEET_SPECS
        )
        target_rows: dict[str, list[dict[str, object]]] = {
            name: [] for name in specs if name != "00_导入说明"
        }
        target_origins: dict[str, list[_AdaptedRowOrigin]] = {
            name: [] for name in specs if name != "00_导入说明"
        }
        used_sources: dict[str, ProtectedImportSourceRef] = {}
        for sheet_mapping in profile.sheets:
            source_pair = sources.get(sheet_mapping.source_ref)
            if source_pair is None:
                raise SourceMappingError("SOURCE_FILE_REQUIRED")
            source, content = source_pair
            if source.opaque_ref != sheet_mapping.source_ref:
                raise SourceMappingError("SOURCE_REFERENCE_MISMATCH")
            try:
                inspection = self._inspector.inspect(BytesIO(content), source=source)
            except ImportWorkbookRejected:
                raise SourceMappingError("SOURCE_WORKBOOK_REJECTED") from None
            worksheet_summary = next(
                (item for item in inspection.worksheets if item.name == sheet_mapping.source_sheet),
                None,
            )
            if worksheet_summary is None:
                raise SourceMappingError("SOURCE_SHEET_REQUIRED")
            if len(set(worksheet_summary.headers)) != len(worksheet_summary.headers):
                raise SourceMappingError("SOURCE_HEADER_DUPLICATE")
            headers = frozenset(worksheet_summary.headers)
            for mapping in sheet_mapping.columns:
                required_headers = (mapping.source_header, *mapping.additional_source_headers)
                if mapping.constant is None and any(
                    header not in headers for header in required_headers
                ):
                    raise SourceMappingError("SOURCE_COLUMN_REQUIRED")
                if mapping.target_header not in specs[sheet_mapping.target_sheet].headers:
                    raise SourceMappingError("TARGET_COLUMN_INVALID")
                if mapping.value_map and mapping.target_header in {
                    "状态",
                    "学生状态",
                    "在校状态",
                    "居住方式",
                    "任职类型",
                    "可用状态",
                    "主要程度",
                    "可见分类",
                    "制度状态",
                }:
                    raise SourceMappingError("STATUS_PROFILE_REQUIRED")
            existing = used_sources.get(sheet_mapping.source_slot)
            if existing is not None and existing.opaque_ref != source.opaque_ref:
                raise SourceMappingError("SOURCE_SLOT_MISMATCH")
            used_sources[sheet_mapping.source_slot] = source
            self._append_rows(
                content=content,
                source_sha256=source.source_sha256,
                mapping=sheet_mapping,
                target_rows=target_rows[sheet_mapping.target_sheet],
                target_origins=target_origins[sheet_mapping.target_sheet],
            )

        if {source.opaque_ref for source in used_sources.values()} != set(sources):
            raise SourceMappingError("SOURCE_REFERENCE_UNUSED")

        standardized = self._standard_workbook(
            target_rows, template_version=profile.template_version
        )
        source_manifest = tuple(
            SchoolFactSourceArtifact(
                logical_source_code=source_slot,
                source_sha256=source.source_sha256,
                size_bytes=source.size_bytes,
            )
            for source_slot, source in sorted(used_sources.items())
        )
        combined_raw_sha = sha256(
            json.dumps(
                [
                    {
                        "logical_source_code": item.logical_source_code,
                        "source_sha256": item.source_sha256,
                        "size_bytes": item.size_bytes,
                    }
                    for item in source_manifest
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        standard_source = self._protected_standard_source(
            standardized,
            opaque_ref=f"adapted:{combined_raw_sha}",
            template_version=profile.template_version,
        )
        try:
            plan = SchoolFactWorkbookParser().parse(
                BytesIO(standardized),
                source=standard_source,
                base_version=base_version,
                mapping_profile=profile.status_profile,
            )
        except SchoolFactWorkbookIssue as exc:
            origin = self._target_origin(
                target_origins,
                target_sheet=exc.sheet_code,
                target_row_number=exc.row_number,
            )
            raise SchoolFactWorkbookIssue(
                code=exc.code,
                source_slot=origin.source_slot,
                sheet_code=origin.source_sheet_code,
                row_number=origin.source_row_number,
                field_code=self._source_field_code(origin, exc.field_code),
                mapping_version=exc.mapping_version,
            ) from None
        flattened_origins = tuple(
            origin
            for sheet_name in specs
            if sheet_name != "00_导入说明"
            for origin in target_origins[sheet_name]
        )
        if len(flattened_origins) != len(plan.change_set.changes):
            raise SourceMappingError("SOURCE_LINEAGE_MISMATCH")
        return SchoolFactWorkbookPlan(
            template_version=plan.template_version,
            raw_source_sha256=combined_raw_sha,
            mapping_version=profile.version,
            mapping_config_sha256=source_mapping_profile_sha256(profile),
            standard_artifact_size_bytes=len(standardized),
            change_set=plan.change_set,
            status_decisions=tuple(
                self._translated_status_decision(item, target_origins)
                for item in plan.status_decisions
            ),
            source_manifest=source_manifest,
            row_lineage=tuple(
                SchoolFactRowProvenance(
                    fact_kind=change.fact_kind,
                    fact_source_key=change.source_key,
                    raw_source_sha256=origin.raw_source_sha256,
                    source_slot=origin.source_slot,
                    source_sheet_code=origin.source_sheet_code,
                    source_row_number=origin.source_row_number,
                    target_field_codes=origin.target_field_codes,
                    source_field_codes=origin.source_field_codes,
                )
                for change, origin in zip(
                    plan.change_set.changes,
                    flattened_origins,
                    strict=True,
                )
            ),
        )

    def _append_rows(
        self,
        *,
        content: bytes,
        source_sha256: str,
        mapping: SourceSheetMapping,
        target_rows: list[dict[str, object]],
        target_origins: list[_AdaptedRowOrigin],
    ) -> None:
        workbook = load_workbook(
            BytesIO(content), read_only=True, data_only=False, keep_links=False
        )
        try:
            rows = workbook[mapping.source_sheet].iter_rows(values_only=True)
            headers = tuple(next(rows))
            for source_row_number, source_values in enumerate(rows, start=2):
                if all(value is None for value in source_values):
                    continue
                source_row = cast(
                    dict[object, object],
                    dict(zip(headers, source_values, strict=True)),
                )
                target_row: dict[str, object] = {}
                for column in mapping.columns:
                    target_row[column.target_header] = self._mapped_value(source_row, column)
                target_rows.append(target_row)
                target_origins.append(
                    self._row_origin(
                        source_sha256=source_sha256,
                        source_row_number=source_row_number,
                        mapping=mapping,
                    )
                )
        finally:
            workbook.close()

    @staticmethod
    def _mapped_value(row: dict[object, object], mapping: SourceColumnMapping) -> object:
        if mapping.constant is not None:
            return mapping.constant
        values = [row.get(mapping.source_header)]
        values.extend(row.get(header) for header in mapping.additional_source_headers)
        if len(values) == 1:
            value = values[0]
        else:
            if any(not isinstance(item, str) for item in values):
                raise SourceMappingError("SOURCE_MERGE_REQUIRES_TEXT")
            value = mapping.join_with.join(item.strip() for item in values if isinstance(item, str))
        if mapping.split_delimiter is not None and mapping.split_index is not None:
            if not isinstance(value, str):
                raise SourceMappingError("SOURCE_SPLIT_REQUIRES_TEXT")
            parts = value.split(mapping.split_delimiter)
            if not -len(parts) <= mapping.split_index < len(parts):
                raise SourceMappingError("SOURCE_SPLIT_INDEX_INVALID")
            value = parts[mapping.split_index].strip()
        if mapping.value_map:
            if not isinstance(value, str):
                raise SourceMappingError("SOURCE_VALUE_MAPPING_REQUIRED")
            mapped = dict(mapping.value_map).get(value.strip())
            if mapped is None:
                raise SourceMappingError("SOURCE_VALUE_MAPPING_REQUIRED")
            value = mapped
        if mapping.datetime_format is not None and mapping.timezone_name is not None:
            try:
                timezone = ZoneInfo(mapping.timezone_name)
            except ZoneInfoNotFoundError:
                raise SourceMappingError("SOURCE_TIMEZONE_INVALID") from None
            if isinstance(value, datetime):
                candidate = value
            elif isinstance(value, str):
                try:
                    candidate = datetime.strptime(value.strip(), mapping.datetime_format)
                except ValueError:
                    raise SourceMappingError("SOURCE_DATETIME_INVALID") from None
            else:
                raise SourceMappingError("SOURCE_DATETIME_INVALID")
            if candidate.tzinfo is None or candidate.utcoffset() is None:
                candidate = candidate.replace(tzinfo=timezone)
            value = candidate.isoformat()
        return value

    @classmethod
    def _row_origin(
        cls,
        *,
        source_sha256: str,
        source_row_number: int,
        mapping: SourceSheetMapping,
    ) -> _AdaptedRowOrigin:
        field_pairs = tuple(
            sorted(
                (
                    target_code,
                    column.source_field_code or target_code,
                )
                for column in mapping.columns
                for target_code in (
                    cls._target_field_code(mapping.target_sheet, column.target_header),
                )
            )
        )
        return _AdaptedRowOrigin(
            raw_source_sha256=source_sha256,
            source_slot=mapping.source_slot,
            source_sheet_code=mapping.source_sheet_code,
            source_row_number=source_row_number,
            target_field_codes=tuple(target for target, _source in field_pairs),
            source_field_codes=tuple(source for _target, source in field_pairs),
        )

    @classmethod
    def _translated_status_decision(
        cls,
        decision: StatusMappingResult,
        target_origins: dict[str, list[_AdaptedRowOrigin]],
    ) -> StatusMappingResult:
        if decision.row_number is None:
            raise SourceMappingError("SOURCE_LINEAGE_MISMATCH")
        origin = cls._target_origin(
            target_origins,
            target_sheet=decision.sheet_code,
            target_row_number=decision.row_number,
        )
        return replace(
            decision,
            source_slot=origin.source_slot,
            sheet_code=origin.source_sheet_code,
            row_number=origin.source_row_number,
            field_code=cls._source_field_code(origin, decision.field_code),
        )

    @staticmethod
    def _target_origin(
        target_origins: dict[str, list[_AdaptedRowOrigin]],
        *,
        target_sheet: str,
        target_row_number: int,
    ) -> _AdaptedRowOrigin:
        candidates = target_origins.get(target_sheet)
        index = target_row_number - 2
        if candidates is None or not 0 <= index < len(candidates):
            raise SourceMappingError("SOURCE_LINEAGE_MISMATCH")
        return candidates[index]

    @staticmethod
    def _source_field_code(origin: _AdaptedRowOrigin, target_field_code: str) -> str:
        try:
            index = origin.target_field_codes.index(target_field_code)
        except ValueError:
            require_neutral_lineage_code(target_field_code)
            return target_field_code
        return origin.source_field_codes[index]

    @staticmethod
    def _standard_workbook(
        target_rows: dict[str, list[dict[str, object]]],
        *,
        template_version: str = SCHOOL_FACT_TEMPLATE_VERSION,
    ) -> bytes:
        specs = (
            F6_SCHOOL_FACT_SHEET_SPECS
            if template_version == "school_facts_v4"
            else SCHOOL_FACT_SHEET_SPECS
        )
        workbook = Workbook()
        active = workbook.active
        if active is None:
            raise RuntimeError("标准工作簿缺少初始工作表。")
        workbook.remove(active)
        for sheet_name, spec in specs.items():
            worksheet = workbook.create_sheet(sheet_name)
            worksheet.append(spec.headers)
            if sheet_name == "00_导入说明":
                worksheet.append([template_version, "由确定性来源适配生成"])
                continue
            for row in target_rows[sheet_name]:
                values = [row.get(header) for header in spec.headers]
                if any(isinstance(value, str) and len(value) > 32_767 for value in values):
                    raise SourceMappingError("SOURCE_CELL_EXCEEDS_EXCEL_CAPACITY")
                worksheet.append(values)
        target = BytesIO()
        workbook.save(target)
        workbook.close()
        return target.getvalue()

    @staticmethod
    def _target_field_code(target_sheet: str, target_header: str) -> str:
        control_codes = {
            "操作": "operation",
            "稳定标识": "source_key",
            "预期版本": "expected_version",
            "生效时间": "effective_at",
        }
        if target_header in control_codes:
            return control_codes[target_header]
        for header, field_code in F6_SCHOOL_FACT_SHEET_SPECS[target_sheet].content_headers:
            if header == target_header:
                return field_code.removeprefix("__").removesuffix("__")
        raise SourceMappingError("TARGET_COLUMN_INVALID")

    @staticmethod
    def _protected_standard_source(
        content: bytes, *, opaque_ref: str, template_version: str = SCHOOL_FACT_TEMPLATE_VERSION
    ) -> ProtectedImportSourceRef:
        specs = (
            F6_SCHOOL_FACT_SHEET_SPECS
            if template_version == "school_facts_v4"
            else SCHOOL_FACT_SHEET_SPECS
        )
        return ProtectedImportSourceRef(
            opaque_ref=opaque_ref,
            source_sha256=sha256(content).hexdigest(),
            filename_summary="adapted-school-facts.xlsx",
            size_bytes=len(content),
            worksheet_names=tuple(specs),
        )


__all__ = [
    "PrivateImportSourceAccessDenied",
    "PrivateImportSourceExpired",
    "PrivateImportSourceStore",
    "PrivateSourceOwner",
    "SchoolFactSourceAdapter",
    "MAX_ADAPTED_SOURCE_COUNT",
    "MAX_ADAPTED_SOURCE_TOTAL_BYTES",
    "ResolvedSourceMappingProfile",
    "SourceColumnMapping",
    "SourceMappingError",
    "SourceMappingProfile",
    "SourceSheetMapping",
    "require_adapted_source_capacity",
    "source_mapping_profile_sha256",
]
