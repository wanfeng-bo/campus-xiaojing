from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO
from typing import IO, BinaryIO, Protocol, cast
from xml.etree.ElementTree import ParseError, iterparse
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from pydantic import ValidationError

from app.platform.master_data_imports.contracts import (
    ColumnMappingDraft,
    ObservedCellType,
    PersonImportUnitDraft,
    ProtectedImportSourceRef,
    StageOrdinaryPersonImportCommand,
    WorkbookInspection,
    WorksheetInspection,
)


class ImportWorkbookRejected(ValueError):
    """表示来源文件或工作簿结构不满足受限导入边界。"""


class _CellLike(Protocol):
    value: object
    data_type: str


class _WorksheetLike(Protocol):
    title: str
    max_row: int
    max_column: int

    def reset_dimensions(self) -> None: ...

    def calculate_dimension(self, force: bool = False) -> str: ...

    def iter_rows(
        self,
        *,
        min_row: int,
        max_row: int,
        max_col: int,
    ) -> Iterator[tuple[_CellLike, ...]]: ...


class RestrictedExcelInspector:
    """只读取受控 xlsx 的结构摘要，不猜映射、不返回样本原文。"""

    def __init__(
        self,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        max_rows: int = 10_000,
        max_columns: int = 100,
        sample_rows: int = 20,
        max_uncompressed_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_rows = max_rows
        self._max_columns = max_columns
        self._sample_rows = sample_rows
        self._max_uncompressed_bytes = max_uncompressed_bytes

    def register(
        self,
        stream: BinaryIO,
        *,
        opaque_ref: str,
        filename_summary: str,
    ) -> ProtectedImportSourceRef:
        """先完成 ZIP/XML 安全检查，再读取工作表名并签发受保护引用。"""

        if not filename_summary.lower().endswith(".xlsx"):
            raise ImportWorkbookRejected("首批 Excel 导入只接受无宏的 xlsx 文件。")
        content = stream.read(self._max_bytes + 1)
        if not content or len(content) > self._max_bytes:
            raise ImportWorkbookRejected("Excel 文件为空或超过允许的字节上限。")
        self._validate_archive(content)
        try:
            workbook = load_workbook(
                BytesIO(content),
                read_only=True,
                data_only=False,
                keep_links=False,
            )
        except (BadZipFile, OSError, ValueError):
            raise ImportWorkbookRejected("Excel 不是有效的 xlsx 文件。") from None
        try:
            worksheet_names = tuple(workbook.sheetnames)
            if any(worksheet.sheet_state != "visible" for worksheet in workbook.worksheets):
                raise ImportWorkbookRejected("Excel 不能包含隐藏工作表。")
        finally:
            workbook.close()
        try:
            source = ProtectedImportSourceRef(
                opaque_ref=opaque_ref,
                source_sha256=sha256(content).hexdigest(),
                filename_summary=filename_summary,
                size_bytes=len(content),
                worksheet_names=worksheet_names,
            )
        except ValidationError:
            raise ImportWorkbookRejected("Excel 来源引用不符合安全约束。") from None
        self.inspect(BytesIO(content), source=source)
        return source

    def inspect(
        self,
        stream: BinaryIO,
        *,
        source: ProtectedImportSourceRef,
    ) -> WorkbookInspection:
        """核对来源摘要并返回不含单元格原文的工作簿结构。"""

        if not source.filename_summary.lower().endswith(".xlsx"):
            raise ImportWorkbookRejected("首批 Excel 导入只接受无宏的 xlsx 文件。")
        content = stream.read(self._max_bytes + 1)
        if len(content) > self._max_bytes:
            raise ImportWorkbookRejected("Excel 文件超过允许的字节上限。")
        if len(content) != source.size_bytes or sha256(content).hexdigest() != source.source_sha256:
            raise ImportWorkbookRejected("Excel 来源大小或摘要与受保护引用不一致。")
        self._validate_archive(content)

        workbook = load_workbook(
            BytesIO(content),
            read_only=True,
            data_only=False,
            keep_links=False,
        )
        try:
            worksheet_names = tuple(workbook.sheetnames)
            if worksheet_names != source.worksheet_names:
                raise ImportWorkbookRejected("Excel 工作表集合与受保护引用不一致。")
            if any(worksheet.sheet_state != "visible" for worksheet in workbook.worksheets):
                raise ImportWorkbookRejected("Excel 不能包含隐藏工作表。")
            worksheets = tuple(
                self._inspect_worksheet(cast(_WorksheetLike, worksheet))
                for worksheet in workbook.worksheets
            )
        finally:
            workbook.close()
        try:
            return WorkbookInspection(source=source, worksheets=worksheets)
        except ValidationError:
            raise ImportWorkbookRejected("Excel 工作簿结构不符合导入约束。") from None

    def _validate_archive(self, content: bytes) -> None:
        """在 openpyxl 前拒绝会让字段归属产生歧义的合并单元格。"""

        try:
            with ZipFile(BytesIO(content)) as archive:
                entries = archive.infolist()
                if sum(entry.file_size for entry in entries) > self._max_uncompressed_bytes:
                    raise ImportWorkbookRejected("Excel 解压后的内容超过安全上限。")
                for entry in entries:
                    normalized_name = entry.filename.lower()
                    if normalized_name.endswith("vbaproject.bin"):
                        raise ImportWorkbookRejected("Excel 不能包含宏。")
                    if normalized_name.startswith("xl/externallinks/"):
                        raise ImportWorkbookRejected("Excel 不能包含外部链接。")
                    if normalized_name.endswith(".rels"):
                        with archive.open(entry) as relationship_stream:
                            self._validate_relationship_xml(relationship_stream)
                    if normalized_name.startswith("xl/worksheets/") and normalized_name.endswith(
                        ".xml"
                    ):
                        with archive.open(entry) as worksheet_stream:
                            self._validate_worksheet_xml(worksheet_stream)
        except BadZipFile as exc:
            raise ImportWorkbookRejected("Excel 不是有效的 xlsx 文件。") from exc

    def _validate_worksheet_xml(self, stream: IO[bytes]) -> None:
        """流式按实际单元格坐标执行预算，并拒绝公式和合并元素。"""

        row_element_count = 0
        last_row_number = 0
        active_row_number: int | None = None
        last_column_number = 0
        try:
            for event, element in iterparse(stream, events=("start", "end")):
                local_name = self._local_name(element.tag)
                if event == "start":
                    if local_name == "f":
                        raise ImportWorkbookRejected("Excel 不能包含公式。")
                    if local_name == "mergeCells":
                        raise ImportWorkbookRejected("Excel 不能包含合并单元格。")
                    if local_name in {"row", "col"} and element.attrib.get("hidden") in {
                        "1",
                        "true",
                        "True",
                    }:
                        raise ImportWorkbookRejected("Excel 不能包含隐藏的数据行或列。")
                    if local_name == "row":
                        row_reference = element.attrib.get("r")
                        if active_row_number is not None:
                            raise ImportWorkbookRejected("Excel 工作表行坐标无效。")
                        row_element_count += 1
                        if row_element_count > self._max_rows:
                            raise ImportWorkbookRejected(
                                f"Excel 工作表行数不能超过 {self._max_rows}。"
                            )
                        row_number = self._bounded_coordinate_number(
                            row_reference,
                            limit=self._max_rows,
                            invalid_message="Excel 工作表行坐标无效。",
                            exceeded_message=(f"Excel 工作表行数不能超过 {self._max_rows}。"),
                        )
                        if row_number <= last_row_number:
                            raise ImportWorkbookRejected("Excel 工作表行坐标重复或顺序无效。")
                        active_row_number = row_number
                        last_row_number = row_number
                        last_column_number = 0
                    if local_name == "c":
                        cell_reference = element.attrib.get("r")
                        match = (
                            re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", cell_reference)
                            if cell_reference is not None
                            else None
                        )
                        if match is None:
                            raise ImportWorkbookRejected("Excel 单元格坐标无效。")
                        column_number = self._bounded_column_number(match.group(1))
                        cell_row_number = self._bounded_coordinate_number(
                            match.group(2),
                            limit=self._max_rows,
                            invalid_message="Excel 单元格坐标无效。",
                            exceeded_message=(f"Excel 工作表行数不能超过 {self._max_rows}。"),
                        )
                        if active_row_number is None or cell_row_number != active_row_number:
                            raise ImportWorkbookRejected("Excel 单元格坐标与所在行不一致。")
                        if column_number <= last_column_number:
                            raise ImportWorkbookRejected("Excel 单元格坐标重复或顺序无效。")
                        last_column_number = column_number
                else:
                    if local_name == "row":
                        active_row_number = None
                    element.clear()
        except ParseError as exc:
            raise ImportWorkbookRejected("Excel 工作表 XML 无效。") from exc

    @staticmethod
    def _bounded_coordinate_number(
        value: str | None,
        *,
        limit: int,
        invalid_message: str,
        exceeded_message: str,
    ) -> int:
        """先做有界词法比较，再转整数，避免恶意超长数字触发解释器异常。"""

        if value is None or re.fullmatch(r"[1-9][0-9]*", value) is None:
            raise ImportWorkbookRejected(invalid_message)
        limit_text = str(limit)
        if len(value) > len(limit_text) or (len(value) == len(limit_text) and value > limit_text):
            raise ImportWorkbookRejected(exceeded_message)
        return int(value)

    def _bounded_column_number(self, column_letters: str) -> int:
        """以当前列预算先限制字母长度，再执行列号换算。"""

        maximum_reference = self._column_letters(self._max_columns)
        if len(column_letters) > len(maximum_reference) or (
            len(column_letters) == len(maximum_reference) and column_letters > maximum_reference
        ):
            raise ImportWorkbookRejected(f"Excel 工作表列数不能超过 {self._max_columns}。")
        return self._column_number(column_letters)

    @classmethod
    def _validate_relationship_xml(cls, stream: IO[bytes]) -> None:
        """拒绝任何 OPC relationship 指向外部目标。"""

        try:
            for event, element in iterparse(stream, events=("start", "end")):
                if event == "start" and cls._local_name(element.tag) == "Relationship":
                    target_mode = next(
                        (
                            value
                            for name, value in element.attrib.items()
                            if cls._local_name(name) == "TargetMode"
                        ),
                        None,
                    )
                    if target_mode is not None and target_mode.casefold() == "external":
                        raise ImportWorkbookRejected("Excel 不能包含外部链接。")
                if event == "end":
                    element.clear()
        except ParseError as exc:
            raise ImportWorkbookRejected("Excel 关系 XML 无效。") from exc

    @staticmethod
    def _local_name(name: str) -> str:
        return name.rsplit("}", 1)[-1]

    @staticmethod
    def _column_number(column_letters: str) -> int:
        number = 0
        for character in column_letters:
            number = number * 26 + ord(character) - ord("A") + 1
        return number

    @staticmethod
    def _column_letters(column_number: int) -> str:
        letters = ""
        remaining = column_number
        while remaining > 0:
            remaining, remainder = divmod(remaining - 1, 26)
            letters = chr(ord("A") + remainder) + letters
        return letters

    def _inspect_worksheet(self, worksheet: _WorksheetLike) -> WorksheetInspection:
        """按有界行列读取一个工作表并汇总类型。"""

        worksheet.reset_dimensions()
        try:
            worksheet.calculate_dimension(force=True)
        except UnboundLocalError:
            raise ImportWorkbookRejected("Excel 工作表不能是完全空白页。") from None
        max_row = int(worksheet.max_row or 0)
        max_column = int(worksheet.max_column or 0)
        if not 1 <= max_row <= self._max_rows:
            raise ImportWorkbookRejected(f"Excel 工作表行数不能超过 {self._max_rows}。")
        if not 1 <= max_column <= self._max_columns:
            raise ImportWorkbookRejected(f"Excel 工作表列数不能超过 {self._max_columns}。")

        rows = worksheet.iter_rows(min_row=1, max_row=max_row, max_col=max_column)
        header_cells = next(rows)
        headers = tuple(self._header_text(cell) for cell in header_cells)
        observed: list[list[ObservedCellType]] = [[] for _ in range(max_column)]
        for row_index, row in enumerate(rows, start=1):
            for column_index, cell in enumerate(row):
                value_type = self._value_type(cell)
                if (
                    row_index <= self._sample_rows
                    and value_type is not None
                    and value_type not in observed[column_index]
                ):
                    observed[column_index].append(value_type)

        try:
            return WorksheetInspection(
                name=worksheet.title,
                row_count=max_row,
                column_count=max_column,
                headers=headers,
                sample_value_types=tuple(tuple(value_types) for value_types in observed),
            )
        except ValidationError:
            raise ImportWorkbookRejected("Excel 工作表结构不符合导入约束。") from None

    @staticmethod
    def _header_text(cell: _CellLike) -> str:
        """要求首行每列都提供可显示的非空字符串表头。"""

        if cell.data_type == "f":
            raise ImportWorkbookRejected("Excel 不能包含公式。")
        value = cell.value
        if not isinstance(value, str) or not value.strip():
            raise ImportWorkbookRejected("Excel 首行每一列都必须是非空文本表头。")
        normalized = value.strip()
        if len(normalized) > 200:
            raise ImportWorkbookRejected("Excel 表头超过允许长度。")
        return normalized

    @staticmethod
    def _value_type(cell: _CellLike) -> ObservedCellType | None:
        """只返回有限类型标签，绝不返回单元格值。"""

        if cell.data_type == "f":
            raise ImportWorkbookRejected("Excel 不能包含公式。")
        value = cell.value
        if value is None:
            return None
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (date, datetime)):
            return "date"
        if isinstance(value, (int, float)):
            return "number"
        return "text"


class RestrictedExcelPersonMapper:
    """按人工确认的列索引重读同一受保护来源，生成类型化人员候选单元。"""

    def __init__(
        self,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        max_rows: int = 10_000,
        max_columns: int = 100,
        max_uncompressed_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._max_bytes = max_bytes
        self._inspector = RestrictedExcelInspector(
            max_bytes=max_bytes,
            max_rows=max_rows,
            max_columns=max_columns,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )

    def map(
        self,
        stream: BinaryIO,
        *,
        source: ProtectedImportSourceRef,
        worksheet_name: str,
        mapping: ColumnMappingDraft,
        default_valid_from: datetime,
    ) -> StageOrdinaryPersonImportCommand:
        """先执行完整安全检查，再按真实 Excel 行号生成草稿；不写数据库。"""

        if default_valid_from.tzinfo is None or default_valid_from.utcoffset() is None:
            raise ImportWorkbookRejected("人员生效时间必须包含明确时区。")
        content = stream.read(self._max_bytes + 1)
        inspection = self._inspector.inspect(BytesIO(content), source=source)
        worksheet_inspection = next(
            (item for item in inspection.worksheets if item.name == worksheet_name),
            None,
        )
        if worksheet_inspection is None:
            raise ImportWorkbookRejected("人工映射引用的工作表不存在。")
        for entry in mapping.entries:
            actual_header = worksheet_inspection.headers[entry.source_column_index - 1]
            if actual_header != entry.source_header:
                raise ImportWorkbookRejected("字段映射引用的表头或列位置已经变化。")

        workbook = load_workbook(
            BytesIO(content),
            read_only=True,
            data_only=False,
            keep_links=False,
        )
        try:
            worksheet = cast(_WorksheetLike, workbook[worksheet_name])
            rows = worksheet.iter_rows(
                min_row=2,
                max_row=worksheet_inspection.row_count,
                max_col=worksheet_inspection.column_count,
            )
            entries = {entry.target_field_code: entry for entry in mapping.entries}
            units: list[PersonImportUnitDraft] = []
            seen_external_references: set[str] = set()
            for row_number, row in enumerate(rows, start=2):
                if all(cell.value is None for cell in row):
                    continue
                display_name = self._required_text(
                    row,
                    column_index=entries["person.display_name"].source_column_index,
                    row_number=row_number,
                    field_name="教职工姓名",
                    max_length=200,
                )
                external_reference = self._required_text(
                    row,
                    column_index=entries["person.external_reference"].source_column_index,
                    row_number=row_number,
                    field_name="校内编号",
                    max_length=120,
                )
                if external_reference in seen_external_references:
                    raise ImportWorkbookRejected(
                        f"Excel 第 {row_number} 行的校内编号与另一数据行重复。"
                    )
                seen_external_references.add(external_reference)
                valid_from = default_valid_from
                valid_from_entry = entries.get("person.valid_from")
                if valid_from_entry is not None:
                    candidate = row[valid_from_entry.source_column_index - 1].value
                    valid_from = self._aware_datetime(candidate, row_number=row_number)
                try:
                    unit = PersonImportUnitDraft(
                        row_number=row_number,
                        display_name=display_name,
                        external_reference=external_reference,
                        valid_from=valid_from,
                    )
                except ValidationError as exc:
                    field_code = str(exc.errors(include_input=False)[0]["loc"][0])
                    safe_field_name = {
                        "display_name": "教职工姓名",
                        "external_reference": "校内编号",
                        "valid_from": "生效时间",
                    }.get(field_code, "人员字段")
                    raise ImportWorkbookRejected(
                        f"Excel 第 {row_number} 行的{safe_field_name}不符合导入约束。"
                    ) from None
                units.append(unit)
        finally:
            workbook.close()
        if not units:
            raise ImportWorkbookRejected("Excel 没有可映射的人员数据行。")
        try:
            return StageOrdinaryPersonImportCommand(
                source=source,
                worksheet_name=worksheet_name,
                mapping=mapping,
                units=tuple(units),
            )
        except ValidationError:
            raise ImportWorkbookRejected("Excel 映射结果不符合导入批次约束。") from None

    @staticmethod
    def _required_text(
        row: tuple[_CellLike, ...],
        *,
        column_index: int,
        row_number: int,
        field_name: str,
        max_length: int,
    ) -> str:
        value = row[column_index - 1].value
        if not isinstance(value, str) or not value.strip():
            raise ImportWorkbookRejected(f"Excel 第 {row_number} 行的{field_name}必须是非空文本。")
        normalized = value.strip()
        if len(normalized) > max_length:
            raise ImportWorkbookRejected(f"Excel 第 {row_number} 行的{field_name}超过允许长度。")
        return normalized

    @staticmethod
    def _aware_datetime(value: object, *, row_number: int) -> datetime:
        """只接受明确含偏移量的 ISO 文本或已带时区的 datetime。"""

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
                raise ImportWorkbookRejected(
                    f"Excel 第 {row_number} 行的生效时间必须是含时区的 ISO-8601 文本。"
                ) from None
        else:
            raise ImportWorkbookRejected(
                f"Excel 第 {row_number} 行的生效时间必须是含时区的 ISO-8601 文本。"
            )
        if candidate.tzinfo is None or candidate.utcoffset() is None:
            raise ImportWorkbookRejected(f"Excel 第 {row_number} 行的生效时间必须包含明确时区。")
        return candidate
