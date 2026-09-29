"""Bounded teaching XLSX interchange; produces a preview, never publishes facts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any, Literal
from zipfile import ZipFile

from app.platform.school_facts.contracts import CanonicalFactChangeV1

from .education_change_planner import EducationFactSnapshot, parse_education_time
from .teaching_contracts import TEACHING_FIELDS


@dataclass(frozen=True)
class TeachingImportPlan:
    changes: tuple[dict[str, Any], ...]
    unchanged_keys: tuple[str, ...]
    replacement_keys: frozenset[str]


def create_teaching_template(kind: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    if kind not in TEACHING_FIELDS:
        raise ValueError("未登记的教学导入类别。")
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "teaching"
    sheet.append(["operation", "source_key", *sorted(TEACHING_FIELDS[kind][0])])
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="244663")
    sheet.freeze_panes = "C2"
    sheet.auto_filter.ref = sheet.dimensions
    from openpyxl.utils import get_column_letter

    for index in range(1, sheet.max_column + 1):
        sheet.column_dimensions[get_column_letter(index)].width = 27
    notes = workbook.create_sheet("说明")
    notes.append(
        [
            "默认增量，省略不删除；空单元格不修改。可选字段用__CLEAR__明确清空；完整替换须在预览时主动选定对象集合与生效时点。"
        ]
    )
    notes.append(
        [
            "source_key是已核实的稳定来源引用，不能按名称猜对象；operation仅upsert或end；数组和人数规则使用JSON。所有修改须完整预览并点击确认。"
        ]
    )
    notes.append(["fact_kind", kind])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def read_teaching_workbook(data: bytes, kind: str) -> tuple[dict[str, Any], ...]:
    from openpyxl import load_workbook

    if kind not in TEACHING_FIELDS or len(data) > 2_000_000:
        raise ValueError("教学工作簿类别或大小无效。")
    with ZipFile(BytesIO(data)) as archive:
        if sum(i.file_size for i in archive.infolist()) > 12_000_000:
            raise ValueError("教学工作簿解压内容超出有限导入容量。")
    workbook = load_workbook(BytesIO(data), read_only=True, data_only=False)
    try:
        if "teaching" not in workbook.sheetnames:
            raise ValueError("缺少teaching工作表。")
        sheet = workbook["teaching"]
        if sheet.max_row > 301 or sheet.max_column > 40:
            raise ValueError("完整教学批次超出容量。")
        rows = sheet.iter_rows()
        header = tuple(c.value for c in next(rows))
        allowed = {"operation", "source_key"} | set(TEACHING_FIELDS[kind][0])
        if (
            len(set(header)) != len(header)
            or not set(header) <= allowed
            or "source_key" not in header
        ):
            raise ValueError("教学模板列不属于指定事实类别。")
        result: list[dict[str, Any]] = []
        for row in rows:
            if any(c.data_type == "f" for c in row):
                raise ValueError("教学导入不接受公式。")
            values = {
                str(key): cell.value
                for key, cell in zip(header, row, strict=True)
                if cell.value is not None
            }
            if values:
                result.append(values)
        return tuple(result)
    finally:
        workbook.close()


def plan_teaching_import(
    kind: str,
    rows: Sequence[Mapping[str, Any]],
    snapshots: tuple[EducationFactSnapshot, ...],
    *,
    effective_at: datetime,
    mode: Literal["incremental", "replace"] = "incremental",
    replacement_keys: frozenset[str] | None = None,
    max_changes: int = 300,
) -> TeachingImportPlan:
    if kind not in TEACHING_FIELDS or mode not in {"incremental", "replace"}:
        raise ValueError("未登记的教学导入方式。")
    if effective_at.tzinfo is None:
        raise ValueError("生效时点须含时区。")
    if mode == "replace" and replacement_keys is None:
        raise ValueError("完整替换必须显式选择完整对象集合。")
    existing = {r.source_key: r for r in snapshots if r.fact_kind == kind}
    if len(existing) != len(snapshots):
        raise ValueError("导入快照须同类别且稳定键唯一。")
    if mode == "replace" and not (replacement_keys or frozenset()) <= existing.keys():
        raise ValueError("完整替换集合未得到完整当前快照。")
    if len(rows) > max_changes:
        raise ValueError("完整教学批次超出容量，不能暗拆。")
    seen: set[str] = set()
    changes: list[dict[str, Any]] = []
    unchanged: list[str] = []
    allowed = TEACHING_FIELDS[kind][0]
    for index, raw in enumerate(rows, 1):
        if not set(raw) <= (allowed | {"operation", "source_key"}):
            raise ValueError("导入列不属于该教学类别。")
        key = str(raw.get("source_key") or "")
        if not key or key in seen:
            raise ValueError("稳定来源键缺失或重复。")
        seen.add(key)
        record = existing.get(key)
        operation = raw.get("operation") or "upsert"
        if operation not in {"upsert", "end"}:
            raise ValueError("教学表格只接受新增更新或明确结束。")
        content = {k: v for k, v in raw.items() if k in allowed and v not in (None, "")}
        for field, value in tuple(content.items()):
            if value == "__CLEAR__":
                content[field] = None
            elif field.endswith("_source_keys") or field == "teaching_role_limits":
                content[field] = json.loads(value) if isinstance(value, str) else value
        if operation == "end":
            if content or record is None:
                raise ValueError("明确结束须选择原对象，且不能携带替换字段。")
            merged = None
        else:
            current = [
                s
                for s in (record.segments if record else ())
                if s.effective_at <= effective_at
                and (s.valid_to is None or effective_at < s.valid_to)
            ]
            before = (
                {**current[0].content, "valid_to": current[0].valid_to}
                if len(current) == 1
                else dict(record.content)
                if record and not record.segments
                else {}
            )
            merged = {**before, **content}
            if kind == "offering_member_source" and "source_kind" in content:
                merged.pop(
                    "class_source_key"
                    if content["source_kind"] == "independent_group"
                    else "group_source_key",
                    None,
                )
            if (
                record
                and not content
                or record
                and {k: v for k, v in merged.items() if v is not None}
                == {k: v for k, v in before.items() if v is not None}
            ):
                unchanged.append(key)
                continue
        change = dict(
            schema_version=3,
            fact_kind=kind,
            source_key=key,
            operation=operation,
            expected_version=record.version if record else 0,
            effective_at=effective_at,
            content=merged,
            origin={"sheet_code": "teaching", "row_number": index},
        )
        canonical = CanonicalFactChangeV1.model_validate_json(json.dumps(change, default=str))
        wire = canonical.model_dump(mode="json", exclude_none=True)
        if merged is not None:
            wire["content"].update({k: None for k, v in merged.items() if v is None})
        changes.append(wire)
    if mode == "replace":
        for key in sorted((replacement_keys or frozenset()) - seen):
            record = existing[key]
            record_end = parse_education_time(record.content.get("valid_to"))
            if record.effective_at >= effective_at or (
                record_end is not None and record_end <= effective_at
            ):
                raise ValueError("完整替换不能暗中取消未来安排或重复结束对象。")
            changes.append(
                dict(
                    schema_version=3,
                    fact_kind=kind,
                    source_key=key,
                    operation="end",
                    expected_version=record.version,
                    effective_at=effective_at.isoformat(),
                    content=None,
                    origin={"sheet_code": "teaching_replace", "row_number": len(changes) + 1},
                )
            )
    if len(changes) > max_changes:
        raise ValueError("完整教学影响超出单批容量，不能暗拆。")
    return TeachingImportPlan(tuple(changes), tuple(unchanged), replacement_keys or frozenset())
