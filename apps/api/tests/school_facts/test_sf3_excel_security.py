from __future__ import annotations

from hashlib import sha256
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from app.platform.master_data_imports.excel import ImportWorkbookRejected, RestrictedExcelInspector


def _content(*, hide: str | None = None) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = "data"
    worksheet.append(["code", "name"])
    worksheet.append(["A", "甲"])
    if hide == "sheet":
        extra = workbook.create_sheet("visible")
        extra.append(["code"])
        worksheet.sheet_state = "hidden"
    elif hide == "row":
        worksheet.row_dimensions[2].hidden = True
    elif hide == "column":
        worksheet.column_dimensions["B"].hidden = True
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    return target.getvalue()


def _source(content: bytes, names: tuple[str, ...]) -> ProtectedImportSourceRef:
    return ProtectedImportSourceRef(
        opaque_ref="server-source-security",
        source_sha256=sha256(content).hexdigest(),
        filename_summary="source.xlsx",
        size_bytes=len(content),
        worksheet_names=names,
    )


@pytest.mark.parametrize("hidden", ["sheet", "row", "column"])
def test_restricted_inspector_rejects_hidden_data(hidden: str) -> None:
    content = _content(hide=hidden)
    names = ("data", "visible") if hidden == "sheet" else ("data",)

    with pytest.raises(ImportWorkbookRejected):
        RestrictedExcelInspector().inspect(BytesIO(content), source=_source(content, names))


def test_restricted_inspector_rejects_source_replacement() -> None:
    original = _content()
    replacement = original + b"different"

    with pytest.raises(ImportWorkbookRejected):
        RestrictedExcelInspector().inspect(
            BytesIO(replacement),
            source=_source(original, ("data",)),
        )
