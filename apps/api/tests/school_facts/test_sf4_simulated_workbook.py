from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import ModuleType

from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from app.platform.master_data_imports.school_facts_excel import (
    SCHOOL_FACT_SHEET_SPECS,
    SCHOOL_FACT_TEMPLATE_VERSION,
    SchoolFactWorkbookParser,
)
from app.platform.school_facts.contracts import SchoolFactKind

REPO_ROOT = Path(__file__).resolve().parents[4]
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_simulated_boarding_school.py"
EXPECTED_FACT_KINDS = frozenset(
    {
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
)


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sf4_generator", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None, "SF4 确定性生成器尚未创建"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parse_workbook(content: bytes, *, base_version: int):
    source = ProtectedImportSourceRef(
        opaque_ref=f"sf4-{sha256(content).hexdigest()[:16]}",
        source_sha256=sha256(content).hexdigest(),
        size_bytes=len(content),
        filename_summary="sf4-simulated-school.xlsx",
        worksheet_names=tuple(SCHOOL_FACT_SHEET_SPECS),
    )
    return SchoolFactWorkbookParser().parse(
        BytesIO(content), source=source, base_version=base_version
    )


def test_sf4_assets_are_deterministic_v2_workbooks_with_declared_ground_truth() -> None:
    generator = _load_generator()
    ground_truth = generator.build_ground_truth()

    assert SCHOOL_FACT_TEMPLATE_VERSION == "school_facts_v2"
    assert ground_truth.as_of.isoformat() == "2026-08-22T12:00:00+08:00"
    assert set(ground_truth.scenario_evidence) == set(range(1, 17))
    assert all(ground_truth.scenario_evidence[index] for index in range(1, 17))

    rendered = generator.render_assets(ground_truth)
    assert set(rendered) == {
        generator.BASE_WORKBOOK_PATH,
        generator.LIFECYCLE_WORKBOOK_PATH,
    }
    for path, expected in rendered.items():
        assert path.is_file(), f"SF4 资产缺失：{path.relative_to(REPO_ROOT)}"
        actual = path.read_bytes()
        assert actual == expected, f"SF4 资产不是生成器的确定性输出：{path.name}"

    # ground truth 是独立冻结输入；生成器只校验，不得覆盖它。
    assert generator.GROUND_TRUTH_PATH.is_file()
    assert (
        generator.GROUND_TRUTH_PATH.read_text(encoding="utf-8")
        == ground_truth.render_human_ground_truth()
    )

    base = _parse_workbook(rendered[generator.BASE_WORKBOOK_PATH], base_version=1)
    lifecycle = _parse_workbook(rendered[generator.LIFECYCLE_WORKBOOK_PATH], base_version=2)
    assert base.template_version == lifecycle.template_version == "school_facts_v2"
    assert base.change_set.changes == ground_truth.base_changes
    assert lifecycle.change_set.changes == ground_truth.lifecycle_changes
    assert base.status_decisions or lifecycle.status_decisions

    assert tuple(SCHOOL_FACT_SHEET_SPECS) == ground_truth.sheet_names
    assert len(ground_truth.sheet_names) == 20
    assert all(change.source_key.startswith("sf4:") for change in ground_truth.all_changes)
    assert all(change.fact_kind in EXPECTED_FACT_KINDS for change in ground_truth.all_changes)


def test_sf4_ground_truth_meets_frozen_scale_and_all_19_fact_kinds() -> None:
    generator = _load_generator()
    ground_truth = generator.build_ground_truth()
    counts = Counter(change.fact_kind for change in ground_truth.base_changes)
    unique_counts = ground_truth.unique_fact_counts

    assert set(unique_counts) == EXPECTED_FACT_KINDS
    assert all(unique_counts[kind] >= 1 for kind in EXPECTED_FACT_KINDS)
    assert unique_counts["school"] == 1
    assert ground_truth.campus_count == 2
    assert ground_truth.staff_count >= 22
    assert ground_truth.student_count == 60
    assert unique_counts["student_school_relationship"] >= 60
    assert ground_truth.dorm_building_count == 2
    assert ground_truth.dorm_floor_count == 4
    assert ground_truth.dorm_room_count == 12
    assert unique_counts["dorm_bed"] == 60
    assert ground_truth.current_resident_count() == 48

    assert counts["person"] >= ground_truth.staff_count + ground_truth.student_count
    assert ground_truth.future_residences
    assert ground_truth.historical_residences
    assert ground_truth.current_residences.isdisjoint(ground_truth.future_residences)
    assert ground_truth.current_residences.isdisjoint(ground_truth.historical_residences)


def test_sf4_ground_truth_expresses_all_frozen_scenarios_without_extra_fact_domains() -> None:
    generator = _load_generator()
    ground_truth = generator.build_ground_truth()

    for index in range(1, 17):
        assert ground_truth.assert_scenario(index), f"SF4 场景 {index} 未命中"

    serialized = ground_truth.render_human_ground_truth()
    forbidden = {
        "duty_code",
        "Governance Grant",
        "班主任关系",
        "行政班",
        "年级事实",
        "单亲家庭",
        "报修工单",
        "维修责任组",
        "审批链",
    }
    assert not {token for token in forbidden if token in serialized}
    assert "FICTIONAL-RESTRICTED-SENTINEL-SF4" in serialized
    assert "example.invalid" in serialized
    assert "000-" in serialized


def test_sf4_time_classification_uses_only_the_fixed_as_of_instant() -> None:
    generator = _load_generator()
    ground_truth = generator.build_ground_truth()

    first = ground_truth.classify_residences(as_of=ground_truth.as_of)
    second = generator.build_ground_truth().classify_residences(as_of=ground_truth.as_of)

    assert first == second
    assert len(first["current"]) == 48
    assert first["future"] == ground_truth.future_residences
    assert first["historical"] == ground_truth.historical_residences
    assert all(path.name.endswith("school_facts_v2.xlsx") for path in ground_truth.workbook_paths)


def test_fact_kind_type_annotation_stays_aligned_with_sf4_ground_truth() -> None:
    # 该赋值只让静态类型检查同时覆盖 SchoolFactKind 与 SF4 的字符串集合。
    kinds: tuple[SchoolFactKind, ...] = tuple(sorted(EXPECTED_FACT_KINDS))  # type: ignore[assignment]
    assert len(kinds) == 19
