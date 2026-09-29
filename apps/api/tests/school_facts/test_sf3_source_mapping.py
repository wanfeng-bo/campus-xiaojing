from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from threading import Barrier

import pytest
from openpyxl import Workbook

from app.composition.school_facts import InMemoryPrivateSchoolFactSourceStore
from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from app.platform.master_data_imports.ports import PrivateImportSourceCapacityExceeded
from app.platform.master_data_imports.school_facts_excel import SCHOOL_FACT_SHEET_SPECS
from app.platform.master_data_imports.source_mapping import (
    PrivateImportSourceAccessDenied,
    PrivateImportSourceExpired,
    PrivateSourceOwner,
    ResolvedSourceMappingProfile,
    SchoolFactSourceAdapter,
    SourceColumnMapping,
    SourceMappingError,
    SourceMappingProfile,
    SourceSheetMapping,
    require_adapted_source_capacity,
    source_mapping_profile_sha256,
)
from app.platform.school_facts.ports import (
    SchoolFactRowProvenance,
    SchoolFactSourceArtifact,
)

NOW = datetime(2026, 8, 20, 8, tzinfo=UTC)
OWNER = PrivateSourceOwner(
    school_id="00000000-0000-0000-0000-000000000001",
    actor_user_id=9,
    session_sha256="a" * 64,
)


def _source_workbook() -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = "学校基本信息(旧表)"
    worksheet.append(["名称", "编码", "使用情况", "版本", "时间", "唯一号"])
    worksheet.append(["候选学校", "XJ-001", "在用", 0, NOW.isoformat(), "school:demo"])
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    return target.getvalue()


def _person_workbook() -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = "staff-any-name"
    worksheet.append(["first", "last", "key", "at"])
    worksheet.append(["静", "张", "person:teacher-1", NOW.isoformat()])
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    return target.getvalue()


def _protected(content: bytes) -> ProtectedImportSourceRef:
    return ProtectedImportSourceRef(
        opaque_ref="server-source-old-school",
        source_sha256=sha256(content).hexdigest(),
        filename_summary="legacy-school.xlsx",
        size_bytes=len(content),
        worksheet_names=("学校基本信息(旧表)",),
    )


def _profile() -> SourceMappingProfile:
    return SourceMappingProfile(
        version="candidate_school_source_v1",
        sheets=(
            SourceSheetMapping(
                source_ref="server-source-old-school",
                source_sheet="学校基本信息(旧表)",
                target_sheet="01_学校",
                source_slot="school_file",
                source_sheet_code="legacy_school",
                columns=(
                    SourceColumnMapping("操作", "操作", constant="upsert"),
                    SourceColumnMapping("唯一号", "稳定标识"),
                    SourceColumnMapping("版本", "预期版本"),
                    SourceColumnMapping("时间", "生效时间"),
                    SourceColumnMapping("编码", "学校编号"),
                    SourceColumnMapping("名称", "学校名称"),
                    SourceColumnMapping("简称", "学校简称", constant="候选校"),
                    SourceColumnMapping("时区", "时区", constant="Asia/Shanghai"),
                    SourceColumnMapping("使用情况", "状态"),
                ),
            ),
        ),
    )


def test_source_adapter_maps_arbitrary_sheet_and_headers_deterministically() -> None:
    content = _source_workbook()
    adapter = SchoolFactSourceAdapter()

    first = adapter.adapt(
        sources={"server-source-old-school": (_protected(content), content)},
        profile=_profile(),
        base_version=1,
    )
    second = adapter.adapt(
        sources={"server-source-old-school": (_protected(content), content)},
        profile=_profile(),
        base_version=1,
    )

    assert first.change_set.model_dump_json() == second.change_set.model_dump_json()
    assert first.mapping_version == "candidate_school_source_v1"
    assert first.change_set.changes[0].content is not None
    assert first.change_set.changes[0].content.display_name == "候选学校"


def test_source_adapter_targets_v2_relation_sheet_and_valid_to_without_a_second_writer() -> None:
    content = _source_workbook()
    profile = SourceMappingProfile(
        version="organization_campus_v2",
        sheets=(
            SourceSheetMapping(
                source_ref="server-source-old-school",
                source_sheet="学校基本信息(旧表)",
                target_sheet="18_组织校区关系",
                source_slot="school_file",
                source_sheet_code="legacy_school",
                columns=(
                    SourceColumnMapping("operation", "操作", constant="upsert"),
                    SourceColumnMapping(
                        "relationship_key",
                        "稳定标识",
                        constant="organization-campus:office:south",
                    ),
                    SourceColumnMapping("version", "预期版本", constant=0),
                    SourceColumnMapping("时间", "生效时间"),
                    SourceColumnMapping(
                        "organization_key",
                        "组织稳定标识",
                        constant="organization:office",
                    ),
                    SourceColumnMapping(
                        "campus_key",
                        "校区稳定标识",
                        constant="location:campus:south",
                    ),
                    SourceColumnMapping(
                        "valid_to",
                        "结束时间",
                        constant=(NOW + timedelta(days=30)).isoformat(),
                    ),
                ),
            ),
        ),
    )

    plan = SchoolFactSourceAdapter().adapt(
        sources={"server-source-old-school": (_protected(content), content)},
        profile=profile,
        base_version=1,
    )

    change = plan.change_set.changes[0]
    assert change.fact_kind == "organization_campus_relationship"
    assert change.content is not None
    assert change.content.organization_source_key == "organization:office"
    assert change.content.campus_source_key == "location:campus:south"
    assert change.content.valid_to == NOW + timedelta(days=30)


def test_adapted_source_capacity_allows_two_6_mib_files_and_rejects_count_or_aggregate() -> None:
    def source(index: int, size_bytes: int) -> ProtectedImportSourceRef:
        return ProtectedImportSourceRef(
            opaque_ref=f"server-source-{index}",
            source_sha256=f"{index:x}" * 64,
            filename_summary=f"source-{index}.xlsx",
            size_bytes=size_bytes,
            worksheet_names=("source_sheet",),
        )

    require_adapted_source_capacity(tuple(source(index, 6 * 1024 * 1024) for index in (1, 2)))

    with pytest.raises(SourceMappingError) as aggregate_error:
        require_adapted_source_capacity(
            tuple(source(index, 9 * 1024 * 1024) for index in (1, 2, 3, 4))
        )
    with pytest.raises(SourceMappingError) as count_error:
        require_adapted_source_capacity(tuple(source(index, 1) for index in range(1, 10)))

    assert aggregate_error.value.code == "SOURCE_SET_CAPACITY_EXCEEDED"
    assert count_error.value.code == "SOURCE_SET_CAPACITY_EXCEEDED"


def test_source_adapter_rejects_unmapped_or_name_guessed_columns() -> None:
    content = _source_workbook()
    bad = SourceMappingProfile(
        version="bad_v1",
        sheets=(
            SourceSheetMapping(
                source_ref="server-source-old-school",
                source_sheet="学校基本信息(旧表)",
                target_sheet="01_学校",
                source_slot="school_file",
                source_sheet_code="legacy_school",
                columns=(SourceColumnMapping("学校名字", "学校名称"),),
            ),
        ),
    )

    with pytest.raises(SourceMappingError) as caught:
        SchoolFactSourceAdapter().adapt(
            sources={"server-source-old-school": (_protected(content), content)},
            profile=bad,
            base_version=1,
        )

    assert caught.value.code == "SOURCE_COLUMN_REQUIRED"
    assert "学校名字" not in str(caught.value)


def test_source_adapter_merges_multiple_files_and_uses_explicit_merge_and_value_map() -> None:
    school_content = _source_workbook()
    person_content = _person_workbook()
    person_source = ProtectedImportSourceRef(
        opaque_ref="server-source-person",
        source_sha256=sha256(person_content).hexdigest(),
        filename_summary="staff.xlsx",
        size_bytes=len(person_content),
        worksheet_names=("staff-any-name",),
    )
    profile = SourceMappingProfile(
        version="multi_file_v1",
        sheets=(
            _profile().sheets[0],
            SourceSheetMapping(
                source_ref="server-source-person",
                source_sheet="staff-any-name",
                target_sheet="02_人员",
                source_slot="staff_file",
                source_sheet_code="legacy_staff",
                columns=(
                    SourceColumnMapping("operation", "操作", constant="upsert"),
                    SourceColumnMapping("key", "稳定标识"),
                    SourceColumnMapping("version", "预期版本", constant=0),
                    SourceColumnMapping("at", "生效时间"),
                    SourceColumnMapping("number", "人员编号", constant="T-001"),
                    SourceColumnMapping(
                        "last",
                        "姓名",
                        additional_source_headers=("first",),
                    ),
                    SourceColumnMapping("kind", "人员类型", constant="staff"),
                    SourceColumnMapping(
                        "state",
                        "状态",
                        constant="在岗",
                    ),
                ),
            ),
        ),
    )

    plan = SchoolFactSourceAdapter().adapt(
        sources={
            "server-source-old-school": (_protected(school_content), school_content),
            "server-source-person": (person_source, person_content),
        },
        profile=profile,
        base_version=1,
    )

    assert [change.fact_kind for change in plan.change_set.changes] == ["school", "person"]
    assert plan.change_set.changes[1].content is not None
    assert plan.change_set.changes[1].content.display_name == "张静"
    assert {decision.standard_value for decision in plan.status_decisions} >= {"active"}
    assert {item.logical_source_code for item in plan.source_manifest} == {
        "school_file",
        "staff_file",
    }
    assert [item.source_sheet_code for item in plan.row_lineage] == [
        "legacy_school",
        "legacy_staff",
    ]
    assert [item.source_row_number for item in plan.row_lineage] == [2, 2]


def test_private_source_store_uses_server_ref_and_expires_at_24_hours() -> None:
    store = InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW)
    content = _source_workbook()

    source = store.put(filename_summary="legacy-school.xlsx", content=content, owner=OWNER)

    assert source.opaque_ref.startswith("sf3src_")
    assert source.opaque_ref != source.source_sha256
    assert store.open(source.opaque_ref, owner=OWNER, now=NOW).read() == content
    with pytest.raises(PrivateImportSourceExpired):
        store.open(source.opaque_ref, owner=OWNER, now=NOW + timedelta(hours=24))
    assert store.contains(source.opaque_ref, owner=OWNER) is False


def test_private_source_describe_reads_only_metadata_without_opening_or_copying_bytes() -> None:
    class _OpenForbiddenStore(InMemoryPrivateSchoolFactSourceStore):
        def open(self, *args: object, **kwargs: object) -> BytesIO:
            del args, kwargs
            raise AssertionError("describe must not open or copy private bytes")

    store = _OpenForbiddenStore(clock=lambda: NOW)
    source = store.put(
        filename_summary="legacy-school.xlsx",
        content=_source_workbook(),
        owner=OWNER,
    )

    described = store.describe(source.opaque_ref, owner=OWNER, now=NOW)

    assert described == source


def test_private_source_store_deletes_bytes_explicitly() -> None:
    store = InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW)
    source = store.put(
        filename_summary="legacy-school.xlsx", content=_source_workbook(), owner=OWNER
    )

    store.delete(source.opaque_ref, owner=OWNER)

    assert store.contains(source.opaque_ref, owner=OWNER) is False
    with pytest.raises(KeyError):
        store.open(source.opaque_ref, owner=OWNER, now=NOW)


def test_private_source_store_purges_expired_bytes_without_an_open() -> None:
    store = InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW)
    source = store.put(
        filename_summary="legacy-school.xlsx", content=_source_workbook(), owner=OWNER
    )

    assert store.purge_expired(now=NOW + timedelta(hours=24)) == 1
    assert store.contains(source.opaque_ref, owner=OWNER) is False


def test_private_source_store_validates_dangerous_workbook_before_storing() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.append(["formula"])
    worksheet.append(["=1+1"])
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    store = InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW)

    with pytest.raises(Exception) as caught:
        store.put(filename_summary="dangerous.xlsx", content=target.getvalue(), owner=OWNER)

    assert caught.type.__name__ == "ImportWorkbookRejected"


def test_private_source_store_rejects_cross_school_or_session_bearer_reuse() -> None:
    store = InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW)
    source = store.put(
        filename_summary="legacy-school.xlsx", content=_source_workbook(), owner=OWNER
    )
    attacker = PrivateSourceOwner(
        school_id="00000000-0000-0000-0000-000000000099",
        actor_user_id=9,
        session_sha256="b" * 64,
    )

    with pytest.raises(PrivateImportSourceAccessDenied):
        store.open(source.opaque_ref, owner=attacker, now=NOW)


def test_private_source_store_enforces_quota_atomically_under_parallel_uploads() -> None:
    barrier = Barrier(2)

    class _BarrierInspector:
        def __init__(self) -> None:
            from app.platform.master_data_imports.excel import RestrictedExcelInspector

            self._delegate = RestrictedExcelInspector(max_rows=10_001)

        def register(self, *args: object, **kwargs: object) -> ProtectedImportSourceRef:
            result = self._delegate.register(*args, **kwargs)  # type: ignore[arg-type]
            barrier.wait(timeout=5)
            return result

    store = InMemoryPrivateSchoolFactSourceStore(
        clock=lambda: NOW,
        inspector=_BarrierInspector(),  # type: ignore[arg-type]
        max_records=1,
        max_owner_records=1,
    )
    content = _source_workbook()

    def upload(index: int) -> ProtectedImportSourceRef | Exception:
        try:
            return store.put(
                filename_summary=f"legacy-school-{index}.xlsx",
                content=content,
                owner=OWNER,
            )
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(upload, (1, 2)))

    assert sum(isinstance(item, ProtectedImportSourceRef) for item in outcomes) == 1
    assert sum(isinstance(item, PrivateImportSourceCapacityExceeded) for item in outcomes) == 1


def test_source_adapter_rejects_duplicate_source_headers() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = "duplicate"
    worksheet.append(["name", "name"])
    worksheet.append(["甲", "乙"])
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    content = target.getvalue()
    source = ProtectedImportSourceRef(
        opaque_ref="duplicate-source",
        source_sha256=sha256(content).hexdigest(),
        filename_summary="duplicate.xlsx",
        size_bytes=len(content),
        worksheet_names=("duplicate",),
    )
    profile = SourceMappingProfile(
        version="duplicate_v1",
        sheets=(
            SourceSheetMapping(
                source_ref="duplicate-source",
                source_sheet="duplicate",
                target_sheet="01_学校",
                source_slot="duplicate_file",
                source_sheet_code="duplicate_sheet",
                columns=(SourceColumnMapping("name", "学校名称"),),
            ),
        ),
    )

    with pytest.raises(SourceMappingError) as caught:
        SchoolFactSourceAdapter().adapt(
            sources={"duplicate-source": (source, content)}, profile=profile, base_version=1
        )

    assert caught.value.code == "SOURCE_HEADER_DUPLICATE"


def test_source_adapter_explicitly_cleans_naive_school_datetime() -> None:
    mapping = SourceColumnMapping(
        "at",
        "生效时间",
        datetime_format="%Y/%m/%d %H:%M",
        timezone_name="Asia/Shanghai",
    )

    assert SchoolFactSourceAdapter._mapped_value({"at": "2026/08/20 08:00"}, mapping) == (
        "2026-08-20T08:00:00+08:00"
    )


def test_mapping_digest_rejects_same_version_with_different_config() -> None:
    original = _profile()
    changed = SourceMappingProfile(
        version=original.version,
        sheets=(
            SourceSheetMapping(
                source_ref=original.sheets[0].source_slot,
                source_sheet=original.sheets[0].source_sheet,
                target_sheet=original.sheets[0].target_sheet,
                source_slot=original.sheets[0].source_slot,
                source_sheet_code=original.sheets[0].source_sheet_code,
                columns=(
                    *original.sheets[0].columns[:-1],
                    SourceColumnMapping("使用情况", "状态", constant="停用"),
                ),
            ),
        ),
    )

    assert source_mapping_profile_sha256(original) != source_mapping_profile_sha256(changed)
    with pytest.raises(ValueError):
        ResolvedSourceMappingProfile(
            school_id=OWNER.school_id,
            source_slots=(original.sheets[0].source_slot,),
            profile=changed,
            config_sha256=source_mapping_profile_sha256(original),
        )


def test_mapping_digest_ignores_only_the_ephemeral_source_reference() -> None:
    original = _profile()
    rebound = SourceMappingProfile(
        version=original.version,
        sheets=(
            SourceSheetMapping(
                source_ref="fresh-private-ref",
                source_sheet=original.sheets[0].source_sheet,
                target_sheet=original.sheets[0].target_sheet,
                source_slot=original.sheets[0].source_slot,
                source_sheet_code=original.sheets[0].source_sheet_code,
                columns=original.sheets[0].columns,
            ),
        ),
    )

    assert source_mapping_profile_sha256(original) == source_mapping_profile_sha256(rebound)


def test_formal_target_sheet_names_are_not_inferred() -> None:
    assert tuple(SCHOOL_FACT_SHEET_SPECS) == (
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


@pytest.mark.parametrize(
    ("field_name", "malicious_value"),
    [
        ("source_slot", "学生花名册.xlsx"),
        ("source_sheet_code", "员工旧表"),
        ("source_slot", "staff file"),
        ("source_sheet_code", "legacy.staff"),
    ],
)
def test_source_mapping_rejects_raw_names_as_long_lived_lineage_codes(
    field_name: str,
    malicious_value: str,
) -> None:
    arguments = {
        "source_ref": "server-source-old-school",
        "source_sheet": "学校基本信息(旧表)",
        "target_sheet": "01_学校",
        "source_slot": "school_file",
        "source_sheet_code": "legacy_school",
        "columns": (SourceColumnMapping("名称", "学校名称"),),
    }
    arguments[field_name] = malicious_value

    with pytest.raises(ValueError):
        SourceSheetMapping(**arguments)  # type: ignore[arg-type]


def test_persisted_source_and_row_lineage_require_neutral_ascii_codes() -> None:
    with pytest.raises(ValueError):
        SchoolFactSourceArtifact(
            logical_source_code="真实学生表.xlsx",
            source_sha256="a" * 64,
            size_bytes=1,
        )
    with pytest.raises(ValueError):
        SchoolFactRowProvenance(
            fact_kind="school",
            fact_source_key="school:demo",
            raw_source_sha256="a" * 64,
            source_slot="school_file",
            source_sheet_code="学校旧表",
            source_row_number=2,
            target_field_codes=("display_name", "姓名"),
            source_field_codes=("school_name", "raw_name"),
        )
