from hashlib import sha256
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.platform.master_data_imports import school_facts_excel as excel
from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from app.platform.master_data_imports.excel import ImportWorkbookRejected
from app.platform.school_facts.contracts import safe_content_payload


def parse(version, sheet, values):
    if sheet == "04_学生在校状态":
        values = {
            "学号": "S001",
            "主要校区稳定标识": "campus:one",
            "行政校区稳定标识": "campus:one",
            **values,
        }
    specs = (
        excel.F6_SCHOOL_FACT_SHEET_SPECS
        if version == "school_facts_v4"
        else excel.SCHOOL_FACT_SHEET_SPECS
    )
    book = Workbook()
    book.remove(book.active)
    for name, spec in specs.items():
        page = book.create_sheet(name)
        page.append(spec.headers)
        if name == "00_导入说明":
            page.append([version, "synthetic"])
        if name == sheet:
            row = {
                "操作": "upsert",
                "稳定标识": "synthetic:one",
                "预期版本": 0,
                "生效时间": "2026-09-22T00:00:00Z",
                **values,
            }
            page.append([row.get(header) for header in spec.headers])
    stream = BytesIO()
    book.save(stream)
    data = stream.getvalue()
    source = ProtectedImportSourceRef(
        opaque_ref="f6-synthetic",
        source_sha256=sha256(data).hexdigest(),
        filename_summary="f6.xlsx",
        size_bytes=len(data),
        worksheet_names=tuple(specs),
    )
    return excel.SchoolFactWorkbookParser().parse(BytesIO(data), source=source, base_version=1)


def test_f6_building_import_keeps_source_and_explicit_template_version():
    plan = parse(
        "school_facts_v4",
        "22_宿舍用途",
        {
            "地点稳定标识": "location:building",
            "用途类别": "building",
            "名称": "住宿楼",
            "是否可用": True,
        },
    )
    assert plan.template_version == "school_facts_v4"
    assert plan.change_set.schema_version == 4
    change = plan.change_set.changes[0]
    assert change.fact_kind == "dormitory_building_profile"
    assert safe_content_payload(change.content)["usage_kind"] == "building"
    assert plan.row_lineage[0].fact_source_key == "synthetic:one"


@pytest.mark.parametrize("marker", ["unknown", "未知"])
def test_explicit_unknown_is_versioned_and_survives_hash_and_lineage(marker):
    plan = parse(
        "school_facts_v4",
        "04_学生在校状态",
        {"学生稳定标识": "student:one", "在校状态": "enrolled", "居住方式": marker},
    )
    change = plan.change_set.changes[0]
    assert "residence_mode" in change.content.model_fields_set
    assert safe_content_payload(change.content)["residence_mode"] is None
    assert "residence_mode" in plan.row_lineage[0].target_field_codes


@pytest.mark.parametrize(
    "version,marker",
    [("school_facts_v2", "unknown"), ("school_facts_v2", None), ("school_facts_v4", None)],
)
def test_blank_or_legacy_unknown_does_not_clear_formal_value(version, marker):
    with pytest.raises(ImportWorkbookRejected):
        parse(
            version,
            "04_学生在校状态",
            {"学生稳定标识": "student:one", "在校状态": "enrolled", "居住方式": marker},
        )


def test_admin_explicit_unknown_preserves_intent_in_versioned_plan():
    from datetime import UTC, datetime

    item = excel.SchoolFactAdminChangeV1.model_validate(
        dict(
            schema_version=4,
            operation="upsert",
            fact_kind="student_school_relationship",
            source_key="school:one",
            expected_version=1,
            effective_at=datetime.now(UTC),
            content=dict(
                student_source_key="student:one",
                student_number="S001",
                enrollment_status="enrolled",
                residence_mode=None,
                primary_campus_source_key="campus:one",
                administrative_campus_source_key="campus:one",
            ),
        )
    )
    plan = excel.SchoolFactAdminPlanBuilder().build_from_inputs(base_version=1, inputs=(item,))
    assert plan.schema_version == 4
    assert safe_content_payload(plan.changes[0].content)["residence_mode"] is None


def test_http_admin_v4_and_null_difference_keep_explicit_unknown():
    from datetime import UTC, datetime

    from app.interfaces.http.school_fact_schemas import (
        SchoolFactAdminChangeRequest,
        SchoolFactFieldDiffResponse,
    )
    from app.interfaces.http.school_facts import _safe_diff_value

    item = SchoolFactAdminChangeRequest.model_validate(
        dict(
            schema_version=4,
            operation="upsert",
            fact_kind="student_school_relationship",
            source_key="school:one",
            expected_version=1,
            effective_at=datetime.now(UTC),
            content={"residence_mode": None},
        )
    )
    assert item.model_dump(exclude_none=True)["content"]["residence_mode"] is None
    assert _safe_diff_value(None) is None
    assert (
        SchoolFactFieldDiffResponse(field_code="residence_mode", new_value=None).new_value is None
    )


def test_source_mapping_v4_binds_template_without_changing_legacy_default():
    from app.platform.master_data_imports.source_mapping import (
        SourceColumnMapping,
        SourceMappingProfile,
        SourceSheetMapping,
        source_mapping_profile_sha256,
    )

    sheet = SourceSheetMapping(
        source_ref="synthetic:one",
        source_sheet="source",
        target_sheet="04_学生在校状态",
        columns=(SourceColumnMapping(source_header="方式", target_header="居住方式"),),
    )
    old = SourceMappingProfile(version="one", sheets=(sheet,))
    new = SourceMappingProfile(version="one", sheets=(sheet,), template_version="school_facts_v4")
    assert source_mapping_profile_sha256(old) != source_mapping_profile_sha256(new)


def test_source_adapter_v4_building_round_trip_preserves_lineage():
    from app.platform.master_data_imports.source_mapping import (
        SchoolFactSourceAdapter,
        SourceColumnMapping,
        SourceMappingProfile,
        SourceSheetMapping,
    )

    values = {
        "操作": "upsert",
        "稳定标识": "building:one",
        "预期版本": 0,
        "生效时间": "2026-09-22T00:00:00Z",
        "地点稳定标识": "location:one",
        "用途类别": "building",
        "名称": "住宿楼",
        "是否可用": True,
    }
    book = Workbook()
    sheet = book.active
    sheet.title = "source"
    sheet.append(list(values))
    sheet.append(list(values.values()))
    out = BytesIO()
    book.save(out)
    content = out.getvalue()
    source = ProtectedImportSourceRef(
        opaque_ref="synthetic:f6",
        source_sha256=sha256(content).hexdigest(),
        filename_summary="source.xlsx",
        size_bytes=len(content),
        worksheet_names=("source",),
    )
    mapping = SourceSheetMapping(
        source_ref=source.opaque_ref,
        source_sheet="source",
        target_sheet="22_宿舍用途",
        columns=tuple(SourceColumnMapping(source_header=k, target_header=k) for k in values),
    )
    with pytest.raises(ValueError):
        SourceMappingProfile(version="test", sheets=(mapping,))
    profile = SourceMappingProfile(
        version="test", sheets=(mapping,), template_version="school_facts_v4"
    )
    result = SchoolFactSourceAdapter().adapt(
        sources={source.opaque_ref: (source, content)}, profile=profile, base_version=1
    )
    assert result.change_set.schema_version == 4
    assert result.change_set.changes[0].fact_kind == "dormitory_building_profile"
    assert result.row_lineage[0].raw_source_sha256 == source.source_sha256
    assert result.row_lineage[0].source_row_number == 2
