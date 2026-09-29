from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from app.platform.master_data_imports.excel import ImportWorkbookRejected
from app.platform.master_data_imports.school_facts_excel import (
    SCHOOL_FACT_ADMIN_MAPPING_VERSION,
    SCHOOL_FACT_SHEET_SPECS,
    SCHOOL_FACT_TEMPLATE_VERSION,
    SchoolFactAdminChangeV1,
    SchoolFactAdminPlanBuilder,
    SchoolFactWorkbookParser,
)

NOW = datetime(2026, 8, 20, 8, tzinfo=UTC)
ROOT_FACT_KINDS = {"school", "person", "student", "organization", "location"}
EXPECTED_FACT_KINDS = {
    "school",
    "person",
    "student",
    "student_school_relationship",
    "login_mapping",
    "organization",
    "organization_campus_relationship",
    "assignment",
    "location",
    "location_alias",
    "dorm_room",
    "dorm_bed",
    "student_bed_residence",
    "person_contact",
    "student_contact",
    "official_contact",
    "policy",
    "policy_scope",
    "policy_version",
}
EXPECTED_V2_SHEETS = (
    "00_导入说明",
    "01_学校",
    "02_人员",
    "03_学生",
    "04_学生在校状态",
    "05_登录映射",
    "06_组织",
    "07_任职",
    "08_地点",
    "09_地点别名",
    "10_宿舍房间属性",
    "11_宿舍床位",
    "12_学生入住",
    "13_个人联系方式",
    "14_学生联系人",
    "15_官方联系方式",
    "16_制度目录",
    "17_制度版本",
    "18_组织校区关系",
    "19_制度适用范围",
)


def _workbook_bytes(
    *,
    reverse_school_columns: bool = False,
    policy_rows: int = 0,
    include_person: bool = False,
) -> bytes:
    workbook = Workbook()
    active = workbook.active
    assert active is not None
    workbook.remove(active)
    for sheet_name, spec in SCHOOL_FACT_SHEET_SPECS.items():
        worksheet = workbook.create_sheet(sheet_name)
        headers = list(spec.headers)
        if reverse_school_columns and sheet_name == "01_学校":
            headers.reverse()
        worksheet.append(headers)
        if sheet_name == "00_导入说明":
            worksheet.append([SCHOOL_FACT_TEMPLATE_VERSION, "由正式通道预检"])
        if sheet_name == "01_学校":
            row = {
                "操作": "upsert",
                "稳定标识": "school:demo",
                "预期版本": 0,
                "生效时间": NOW.isoformat(),
                "学校编号": "XJ-001",
                "学校名称": "候选学校",
                "学校简称": "候选校",
                "时区": "Asia/Shanghai",
                "状态": "正常",
            }
            worksheet.append([row.get(header) for header in headers])
        if sheet_name == "16_制度目录":
            for index in range(1, policy_rows + 1):
                row = {
                    "操作": "upsert",
                    "稳定标识": f"policy:row-limit:{index:05d}",
                    "预期版本": 0,
                    "生效时间": NOW.isoformat(),
                }
                worksheet.append([row.get(header) for header in headers])
        if sheet_name == "02_人员" and include_person:
            row = {
                "操作": "upsert",
                "稳定标识": "person:demo",
                "预期版本": 0,
                "生效时间": NOW.isoformat(),
                "人员编号": "T-001",
                "姓名": "管理员",
                "人员类型": "staff",
                "状态": "在职",
            }
            worksheet.append([row.get(header) for header in headers])
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    return target.getvalue()


def _source(content: bytes, names: tuple[str, ...] | None = None) -> ProtectedImportSourceRef:
    return ProtectedImportSourceRef(
        opaque_ref="server-source-1",
        source_sha256=sha256(content).hexdigest(),
        filename_summary="school-facts.xlsx",
        size_bytes=len(content),
        worksheet_names=names or tuple(SCHOOL_FACT_SHEET_SPECS),
    )


def _total_capacity_workbook_bytes(*, extra_row: bool) -> bytes:
    workbook = Workbook(write_only=True)
    for sheet_name, spec in SCHOOL_FACT_SHEET_SPECS.items():
        worksheet = workbook.create_sheet(sheet_name)
        headers = list(spec.headers)
        worksheet.append(headers)
        if sheet_name == "00_导入说明":
            worksheet.append([SCHOOL_FACT_TEMPLATE_VERSION, "由正式通道预检"])
            continue
        for index in range(1, 10_001):
            row: dict[str, object] | None = None
            if sheet_name == "02_人员":
                row = {
                    "操作": "upsert",
                    "稳定标识": f"person:capacity:{index:05d}",
                    "预期版本": 0,
                    "生效时间": NOW.isoformat(),
                    "姓名": f"人员{index:05d}",
                    "人员类型": "staff",
                    "状态": "在职",
                }
            elif sheet_name == "03_学生":
                row = {
                    "操作": "upsert",
                    "稳定标识": f"student:capacity:{index:05d}",
                    "预期版本": 0,
                    "生效时间": NOW.isoformat(),
                    "人员稳定标识": f"person:capacity:{index:05d}",
                    "学生状态": "正常",
                }
            elif sheet_name == "09_地点别名":
                row = {
                    "操作": "upsert",
                    "稳定标识": f"location-alias:capacity:{index:05d}",
                    "预期版本": 0,
                    "生效时间": NOW.isoformat(),
                    "地点稳定标识": f"location:capacity:{index:05d}",
                    "别名": f"别名{index:05d}",
                    "别名类型": "common",
                }
            elif sheet_name == "11_宿舍床位":
                row = {
                    "操作": "upsert",
                    "稳定标识": f"bed:capacity:{index:05d}",
                    "预期版本": 0,
                    "生效时间": NOW.isoformat(),
                    "房间地点稳定标识": f"room:capacity:{index:05d}",
                    "床位编号": f"B-{index:05d}",
                    "床位名称": f"床位{index:05d}",
                    "可用状态": "可用",
                }
            elif sheet_name == "16_制度目录":
                row = {
                    "操作": "upsert",
                    "稳定标识": f"policy:capacity:{index:05d}",
                    "预期版本": 0,
                    "生效时间": NOW.isoformat(),
                }
            if row is not None:
                worksheet.append([row.get(header) for header in headers])
        if sheet_name == "06_组织" and extra_row:
            row = {
                "操作": "upsert",
                "稳定标识": "organization:capacity:extra",
                "预期版本": 0,
                "生效时间": NOW.isoformat(),
                "组织类型": "department",
                "组织名称": "额外组织",
                "展示路径": "/额外组织",
                "状态": "正常",
            }
            worksheet.append([row.get(header) for header in headers])
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    return target.getvalue()


def test_formal_workbook_accepts_all_exact_sheets_and_column_reordering() -> None:
    content = _workbook_bytes(reverse_school_columns=True)

    plan = SchoolFactWorkbookParser().parse(
        BytesIO(content),
        source=_source(content),
        base_version=1,
    )

    assert plan.template_version == SCHOOL_FACT_TEMPLATE_VERSION
    assert len(SCHOOL_FACT_SHEET_SPECS) == 20
    assert len(plan.change_set.changes) == 1
    assert plan.change_set.changes[0].fact_kind == "school"
    assert plan.change_set.changes[0].content is not None
    assert plan.change_set.changes[0].content.lifecycle_status == "active"
    assert plan.status_decisions[0].mapping_version


def test_v2_registry_exposes_19_fact_kinds_with_exact_legal_operations_and_fields() -> None:
    assert SCHOOL_FACT_TEMPLATE_VERSION == "school_facts_v2"
    assert SCHOOL_FACT_ADMIN_MAPPING_VERSION == "admin_v2"
    assert tuple(SCHOOL_FACT_SHEET_SPECS) == EXPECTED_V2_SHEETS
    specs_by_kind = {
        spec.fact_kind: spec
        for spec in SCHOOL_FACT_SHEET_SPECS.values()
        if spec.fact_kind is not None
    }
    assert set(specs_by_kind) == EXPECTED_FACT_KINDS

    for fact_kind, spec in specs_by_kind.items():
        expected = (
            ("upsert", "end", "revoke", "reactivate")
            if fact_kind in ROOT_FACT_KINDS
            else ("upsert", "end", "revoke")
        )
        assert spec.allowed_operations == expected

    assert dict(specs_by_kind["student_school_relationship"].content_headers) == {
        "学生稳定标识": "student_source_key",
        "学号": "student_number",
        "在校状态": "enrollment_status",
        "居住方式": "residence_mode",
        "主要校区稳定标识": "primary_campus_source_key",
        "行政校区稳定标识": "administrative_campus_source_key",
    }
    assert "显示名" not in dict(specs_by_kind["login_mapping"].content_headers)
    assert dict(specs_by_kind["organization_campus_relationship"].content_headers) == {
        "组织稳定标识": "organization_source_key",
        "校区稳定标识": "campus_source_key",
        "结束时间": "valid_to",
    }
    assert dict(specs_by_kind["assignment"].content_headers)["计划结束时间"] == "valid_to"
    assert dict(specs_by_kind["student_bed_residence"].content_headers) == {
        "学生稳定标识": "student_source_key",
        "床位稳定标识": "bed_source_key",
        "结束时间": "valid_to",
    }
    assert dict(specs_by_kind["policy_scope"].content_headers) == {
        "制度目录稳定标识": "policy_source_key",
        "组织稳定标识": "organization_source_key",
        "校区稳定标识": "campus_source_key",
        "结束时间": "valid_to",
    }


def test_formal_workbook_accepts_reactivate_only_on_a_root_fact_sheet() -> None:
    content = _workbook_bytes()
    workbook = __import__("openpyxl").load_workbook(BytesIO(content))
    worksheet = workbook["01_学校"]
    headers = [cell.value for cell in worksheet[1]]
    worksheet.cell(2, headers.index("操作") + 1).value = "reactivate"
    worksheet.cell(2, headers.index("预期版本") + 1).value = 1
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    changed = target.getvalue()

    plan = SchoolFactWorkbookParser().parse(
        BytesIO(changed), source=_source(changed), base_version=1
    )

    assert plan.change_set.changes[0].operation == "reactivate"


def test_formal_workbook_rejects_hidden_non_root_reactivate_value() -> None:
    content = _workbook_bytes()
    workbook = __import__("openpyxl").load_workbook(BytesIO(content))
    worksheet = workbook["05_登录映射"]
    headers = [cell.value for cell in worksheet[1]]
    row = {
        "操作": "reactivate",
        "稳定标识": "login:forged-reactivate",
        "预期版本": 1,
        "生效时间": NOW.isoformat(),
        "登录提供方": "local-test",
        "租户标识": "school-1",
        "登录主体": "forged",
        "人员稳定标识": "person:demo",
        "状态": "confirmed",
    }
    worksheet.append([row.get(header) for header in headers])
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    changed = target.getvalue()

    with pytest.raises(ImportWorkbookRejected, match="不适用于该事实类别"):
        SchoolFactWorkbookParser().parse(BytesIO(changed), source=_source(changed), base_version=1)


@pytest.mark.parametrize(
    "mutation", ["missing_sheet", "extra_sheet", "unknown_header", "duplicate_header"]
)
def test_formal_workbook_rejects_sheet_or_header_drift(mutation: str) -> None:
    content = _workbook_bytes()
    workbook = __import__("openpyxl").load_workbook(BytesIO(content))
    if mutation == "missing_sheet":
        del workbook["17_制度版本"]
    elif mutation == "extra_sheet":
        workbook.create_sheet("多余页")
    elif mutation == "unknown_header":
        workbook["01_学校"].cell(1, 1).value = "未知列"
    else:
        workbook["01_学校"].cell(1, 2).value = workbook["01_学校"].cell(1, 1).value
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    changed = target.getvalue()

    with pytest.raises(ImportWorkbookRejected):
        SchoolFactWorkbookParser().parse(
            BytesIO(changed),
            source=_source(
                changed,
                tuple(
                    __import__("openpyxl")
                    .load_workbook(BytesIO(changed), read_only=True)
                    .sheetnames
                ),
            ),
            base_version=1,
        )


def test_excel_and_admin_independently_build_same_cross_sheet_canonical_plan() -> None:
    content = _workbook_bytes(include_person=True)
    excel_plan = SchoolFactWorkbookParser().parse(
        BytesIO(content),
        source=_source(content),
        base_version=1,
    )

    admin_plan = SchoolFactAdminPlanBuilder().build_from_inputs(
        base_version=1,
        inputs=(
            SchoolFactAdminChangeV1.model_validate(
                {
                    "operation": "upsert",
                    "fact_kind": "person",
                    "source_key": "person:demo",
                    "expected_version": 0,
                    "effective_at": NOW,
                    "content": {
                        "business_code": "T-001",
                        "display_name": "管理员",
                        "person_kind": "staff",
                        "lifecycle_status": "active",
                    },
                }
            ),
            SchoolFactAdminChangeV1.model_validate(
                {
                    "operation": "upsert",
                    "fact_kind": "school",
                    "source_key": "school:demo",
                    "expected_version": 0,
                    "effective_at": NOW,
                    "content": {
                        "business_code": "XJ-001",
                        "display_name": "候选学校",
                        "short_name": "候选校",
                        "timezone_name": "Asia/Shanghai",
                        "lifecycle_status": "active",
                    },
                }
            ),
        ),
    )

    assert admin_plan.model_dump_json() == excel_plan.change_set.model_dump_json()


def test_policy_status_is_recorded_as_mapping_provenance_not_a_fact_enum_escape() -> None:
    assert "制度状态" in SCHOOL_FACT_SHEET_SPECS["17_制度版本"].headers


def test_end_or_revoke_row_rejects_hidden_replacement_content() -> None:
    content = _workbook_bytes()
    workbook = __import__("openpyxl").load_workbook(BytesIO(content))
    worksheet = workbook["01_学校"]
    headers = [cell.value for cell in worksheet[1]]
    worksheet.cell(2, headers.index("操作") + 1).value = "end"
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    changed = target.getvalue()

    with pytest.raises(ImportWorkbookRejected):
        SchoolFactWorkbookParser().parse(BytesIO(changed), source=_source(changed), base_version=1)


def test_formal_sheet_accepts_exactly_10000_data_rows_beside_the_header() -> None:
    content = _workbook_bytes(policy_rows=10_000)

    plan = SchoolFactWorkbookParser().parse(
        BytesIO(content),
        source=_source(content),
        base_version=1,
    )

    assert len(plan.change_set.changes) == 10_001
    assert sum(change.fact_kind == "policy" for change in plan.change_set.changes) == 10_000


def test_formal_sheet_rejects_10001_data_rows() -> None:
    content = _workbook_bytes(policy_rows=10_001)

    with pytest.raises(ImportWorkbookRejected, match="10001"):
        SchoolFactWorkbookParser().parse(
            BytesIO(content),
            source=_source(content),
            base_version=1,
        )


def test_formal_workbook_accepts_exactly_50000_total_data_rows() -> None:
    content = _total_capacity_workbook_bytes(extra_row=False)

    plan = SchoolFactWorkbookParser().parse(
        BytesIO(content),
        source=_source(content),
        base_version=1,
    )

    assert len(plan.change_set.changes) == 50_000


def test_formal_workbook_rejects_50001_total_rows_without_any_sheet_exceeding_10000() -> None:
    content = _total_capacity_workbook_bytes(extra_row=True)

    with pytest.raises(ImportWorkbookRejected, match="50000"):
        SchoolFactWorkbookParser().parse(
            BytesIO(content),
            source=_source(content),
            base_version=1,
        )
