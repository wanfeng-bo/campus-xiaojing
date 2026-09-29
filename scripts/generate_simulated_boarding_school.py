from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

REPO_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = REPO_ROOT / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.platform.master_data_imports.school_facts_excel import (
    SCHOOL_FACT_SHEET_SPECS,
    SCHOOL_FACT_TEMPLATE_VERSION,
)
from app.platform.school_facts.contracts import (
    CanonicalFactChangeV1,
    CanonicalFactContentV1,
    safe_content_payload,
)

FIXTURE_DIR = REPO_ROOT / "fixtures" / "school_facts"
BASE_WORKBOOK_PATH = (
    FIXTURE_DIR / "sf4_simulated_boarding_school_base_school_facts_v2.xlsx"
)
LIFECYCLE_WORKBOOK_PATH = (
    FIXTURE_DIR / "sf4_simulated_boarding_school_lifecycle_school_facts_v2.xlsx"
)
GROUND_TRUTH_PATH = FIXTURE_DIR / "sf4_fictional_ground_truth.md"

CHINA_TZ = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 8, 22, 12, 0, tzinfo=CHINA_TZ)
BASE_AT = datetime(2026, 8, 1, 8, 0, tzinfo=CHINA_TZ)
SCHOOL_AT = datetime(2024, 1, 1, 8, 0, tzinfo=CHINA_TZ)
ROOT_AT = datetime(2024, 1, 2, 8, 0, tzinfo=CHINA_TZ)
FIXED_ZIP_TIME = (2026, 8, 22, 12, 0, 0)


@dataclass(frozen=True, slots=True)
class _RawChange:
    operation: str
    fact_kind: str
    source_key: str
    expected_version: int
    effective_at: datetime
    content: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ResidenceInterval:
    source_key: str
    student_source_key: str
    bed_source_key: str
    valid_from: datetime
    valid_to: datetime | None
    terminated_at: datetime | None = None
    revoked_at: datetime | None = None

    def classification(self, *, as_of: datetime) -> str:
        if self.valid_from > as_of:
            return "future"
        end_candidates = tuple(
            instant
            for instant in (self.valid_to, self.terminated_at, self.revoked_at)
            if instant is not None
        )
        if end_candidates and min(end_candidates) <= as_of:
            return "historical"
        return "current"


@dataclass(frozen=True, slots=True)
class SimulatedSchoolGroundTruth:
    as_of: datetime
    base_changes: tuple[CanonicalFactChangeV1, ...]
    lifecycle_changes: tuple[CanonicalFactChangeV1, ...]
    residence_intervals: tuple[ResidenceInterval, ...]
    scenario_evidence: dict[int, str]
    staff_keys: tuple[str, ...]
    student_keys: tuple[str, ...]
    campus_keys: tuple[str, ...]
    dorm_building_keys: tuple[str, ...]
    dorm_floor_keys: tuple[str, ...]
    dorm_room_keys: tuple[str, ...]

    @property
    def all_changes(self) -> tuple[CanonicalFactChangeV1, ...]:
        return (*self.base_changes, *self.lifecycle_changes)

    @property
    def sheet_names(self) -> tuple[str, ...]:
        return tuple(SCHOOL_FACT_SHEET_SPECS)

    @property
    def workbook_paths(self) -> tuple[Path, Path]:
        return BASE_WORKBOOK_PATH, LIFECYCLE_WORKBOOK_PATH

    @property
    def unique_fact_counts(self) -> dict[str, int]:
        by_kind: dict[str, set[str]] = {}
        for change in self.all_changes:
            by_kind.setdefault(change.fact_kind, set()).add(change.source_key)
        return {kind: len(keys) for kind, keys in by_kind.items()}

    @property
    def campus_count(self) -> int:
        return len(self.campus_keys)

    @property
    def staff_count(self) -> int:
        return len(self.staff_keys)

    @property
    def student_count(self) -> int:
        return len(self.student_keys)

    @property
    def dorm_building_count(self) -> int:
        return len(self.dorm_building_keys)

    @property
    def dorm_floor_count(self) -> int:
        return len(self.dorm_floor_keys)

    @property
    def dorm_room_count(self) -> int:
        return len(self.dorm_room_keys)

    def classify_residences(self, *, as_of: datetime) -> dict[str, frozenset[str]]:
        buckets: dict[str, set[str]] = {
            "current": set(),
            "future": set(),
            "historical": set(),
        }
        for interval in self.residence_intervals:
            buckets[interval.classification(as_of=as_of)].add(interval.source_key)
        return {kind: frozenset(keys) for kind, keys in buckets.items()}

    @property
    def current_residences(self) -> frozenset[str]:
        return self.classify_residences(as_of=self.as_of)["current"]

    @property
    def future_residences(self) -> frozenset[str]:
        return self.classify_residences(as_of=self.as_of)["future"]

    @property
    def historical_residences(self) -> frozenset[str]:
        return self.classify_residences(as_of=self.as_of)["historical"]

    def current_resident_count(self) -> int:
        current_keys = self.current_residences
        return len(
            {
                interval.student_source_key
                for interval in self.residence_intervals
                if interval.source_key in current_keys
            }
        )

    def assert_scenario(self, index: int) -> bool:
        return bool(self.scenario_evidence.get(index))

    def render_human_ground_truth(self) -> str:
        fact_counts = Counter(change.fact_kind for change in self.all_changes)
        unique_counts = self.unique_fact_counts
        lines = [
            "# SF4 虚构寄宿制学校事实真相",
            "",
            "> 本文件先于数据库结果声明人工合成测试事实；不得反向由数据库结果定义。",
            "> 学校、姓名、编号、联系方式和制度正文均为虚构测试内容，不取材于真实个人或学校。",
            "",
            f"- 唯一事实判定时点：`{self.as_of.isoformat()}`",
            "- 学校：星澜实验寄宿学校（虚构），稳定标识 `sf4:school:xinglan`。",
            "- 校区：东澜校区 `sf4:campus:east`、西汀校区 `sf4:campus:west`。",
            f"- 教职工：{self.staff_count} 人；学生：{self.student_count} 人。",
            "- 正式导入顺序：base 工作簿确认成功后，再确认 lifecycle 工作簿。",
            "- 两份工作簿的拆分原因：同一变更集不允许对同一稳定事实目标重复写入。",
            (
                "- 技术前置：隔离库先建立预绑定学校租户、一个虚构管理 actor 的最小身份/任职上下文，"
                "并预置 24 个虚构登录账号；这些前置不属于本 ground truth 的 455 条 SF4 变更。"
            ),
            "- 正式通道边界：除预绑定学校稳定根外，全部 `sf4:` 来源键及 455 条变更都只来自两份工作簿。",
            "",
            "## 冻结规模",
            "",
            "| 项目 | 数量 |",
            "| --- | ---: |",
            "| 学校 | 1 |",
            f"| 校区 | {self.campus_count} |",
            f"| 教职工 | {self.staff_count} |",
            f"| 学生 | {self.student_count} |",
            f"| 学生—学校关系 | {unique_counts['student_school_relationship']} |",
            f"| 宿舍楼 | {self.dorm_building_count} |",
            f"| 宿舍楼层 | {self.dorm_floor_count} |",
            f"| 宿舍房间 | {self.dorm_room_count} |",
            f"| 床位 | {unique_counts['dorm_bed']} |",
            f"| 判定时点当前入住学生 | {self.current_resident_count()} |",
            "",
            "## 16 项场景证据",
            "",
        ]
        for index in range(1, 17):
            lines.append(f"{index}. {self.scenario_evidence[index]}")
        lines.extend(
            [
                "",
                "## 19 类事实数量",
                "",
                "| Fact Kind | 唯一事实数 | 变更行数 | 代表稳定标识 |",
                "| --- | ---: | ---: | --- |",
            ]
        )
        for kind in sorted(unique_counts):
            representative = next(
                change.source_key
                for change in self.all_changes
                if change.fact_kind == kind
            )
            lines.append(
                f"| `{kind}` | {unique_counts[kind]} | {fact_counts[kind]} | `{representative}` |"
            )
        lines.extend(
            [
                "",
                "## 完整规范事实清单",
                "",
                "以下清单是人工验收权威；数据库只用于验证它，没有反向定义权。",
                "",
            ]
        )
        for phase, changes in (
            ("base", self.base_changes),
            ("lifecycle", self.lifecycle_changes),
        ):
            lines.extend((f"### {phase}", ""))
            for change in changes:
                payload = safe_content_payload(change.content)
                content = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=lambda value: (
                        value.isoformat() if isinstance(value, datetime) else value
                    ),
                )
                lines.append(
                    f"- `{change.fact_kind}` / `{change.source_key}` / "
                    f"`{change.operation}` / v{change.expected_version} / "
                    f"`{change.effective_at.isoformat()}` / {content}"
                )
            lines.append("")
        lines.extend(
            [
                "## 当前、历史与未来入住判定",
                "",
            ]
        )
        classified = self.classify_residences(as_of=self.as_of)
        for label in ("current", "historical", "future"):
            lines.append(
                f"- {label}: "
                + ", ".join(f"`{key}`" for key in sorted(classified[label]))
            )
        lines.append("")
        return "\n".join(lines)


def _dt(year: int, month: int, day: int, hour: int = 8) -> datetime:
    return datetime(year, month, day, hour, 0, tzinfo=CHINA_TZ)


def _finalize(raw_changes: list[_RawChange]) -> tuple[CanonicalFactChangeV1, ...]:
    fact_order = {
        spec.fact_kind: index
        for index, spec in enumerate(SCHOOL_FACT_SHEET_SPECS.values())
        if spec.fact_kind is not None
    }
    origin_by_kind = {
        spec.fact_kind: spec.origin_code
        for spec in SCHOOL_FACT_SHEET_SPECS.values()
        if spec.fact_kind is not None
    }
    indexed = list(enumerate(raw_changes))
    indexed.sort(key=lambda pair: (fact_order[pair[1].fact_kind], pair[0]))
    row_by_kind: Counter[str] = Counter()
    result: list[CanonicalFactChangeV1] = []
    for _index, raw in indexed:
        row_by_kind[raw.fact_kind] += 1
        content = (
            CanonicalFactContentV1.model_validate(raw.content)
            if raw.content is not None
            else None
        )
        result.append(
            CanonicalFactChangeV1(
                operation=raw.operation,
                fact_kind=raw.fact_kind,
                source_key=raw.source_key,
                expected_version=raw.expected_version,
                effective_at=raw.effective_at,
                origin={
                    "sheet_code": origin_by_kind[raw.fact_kind],
                    "row_number": row_by_kind[raw.fact_kind] + 1,
                },
                content=content,
            )
        )
    return tuple(result)


def build_ground_truth() -> SimulatedSchoolGroundTruth:
    base: list[_RawChange] = []
    lifecycle: list[_RawChange] = []

    def add(
        kind: str,
        key: str,
        content: dict[str, Any],
        *,
        effective_at: datetime = BASE_AT,
        expected_version: int = 0,
    ) -> None:
        if effective_at == BASE_AT and kind in {
            "person",
            "student",
            "organization",
            "student_school_relationship",
            "location",
            "dorm_room",
            "dorm_bed",
            "policy",
        }:
            effective_at = ROOT_AT
        base.append(
            _RawChange("upsert", kind, key, expected_version, effective_at, content)
        )

    def change(
        operation: str,
        kind: str,
        key: str,
        *,
        effective_at: datetime,
        content: dict[str, Any] | None = None,
    ) -> None:
        lifecycle.append(_RawChange(operation, kind, key, 1, effective_at, content))

    add(
        "school",
        "sf4:school:xinglan",
        {
            "business_code": "SF4-XLS",
            "display_name": "星澜实验寄宿学校（虚构）",
            "short_name": "星澜虚构校",
            "timezone_name": "Asia/Shanghai",
            "lifecycle_status": "active",
        },
        effective_at=SCHOOL_AT,
        expected_version=1,
    )

    staff_keys = tuple(f"sf4:person:staff:{index:03d}" for index in range(1, 25))
    student_person_keys = tuple(
        f"sf4:person:student:{index:03d}" for index in range(1, 61)
    )
    student_keys = tuple(f"sf4:student:{index:03d}" for index in range(1, 61))
    contact_person_keys = tuple(
        f"sf4:person:contact:{index:03d}" for index in range(1, 7)
    )
    staff_names = (
        "测试教师甲",
        "测试教师乙",
        "测试教师丙",
        "测试教师丁",
        "测试教师戊",
        "测试教师己",
        "测试教师庚",
        "测试教师辛",
        "测试教师壬",
        "测试教师癸",
        "模拟重名",
        "模拟重名",
        "测试教师子",
        "测试教师丑",
        "测试教师寅",
        "测试教师卯",
        "测试教师辰",
        "测试教师巳",
        "测试教师午",
        "测试教师未",
        "测试教师申",
        "测试教师酉",
        "测试教师戌",
        "测试教师亥",
    )
    for index, (key, name) in enumerate(zip(staff_keys, staff_names, strict=True), 1):
        add(
            "person",
            key,
            {
                "business_code": f"SF4-T{index:03d}",
                "display_name": name,
                "person_kind": "staff",
                "lifecycle_status": "active",
            },
        )
    for index, key in enumerate(student_person_keys, 1):
        add(
            "person",
            key,
            {
                "business_code": f"SF4-P{index:03d}",
                "display_name": f"虚构学生{index:02d}",
                "person_kind": "other",
                "lifecycle_status": "active",
            },
        )
    contact_names = (
        "虚构家长甲",
        "虚构外祖父乙",
        "虚构家长丙",
        "虚构家长丁",
        "虚构家长戊",
        "虚构家长己",
    )
    for index, (key, name) in enumerate(
        zip(contact_person_keys, contact_names, strict=True), 1
    ):
        add(
            "person",
            key,
            {
                "business_code": f"SF4-C{index:03d}",
                "display_name": name,
                "person_kind": "student_contact",
                "lifecycle_status": "active",
            },
        )

    for student_key, person_key in zip(student_keys, student_person_keys, strict=True):
        add(
            "student",
            student_key,
            {"person_source_key": person_key, "status": "active"},
        )

    campus_keys = ("sf4:campus:east", "sf4:campus:west")
    for index, student_key in enumerate(student_keys, 1):
        primary = campus_keys[0] if index <= 30 else campus_keys[1]
        administrative = campus_keys[1] if index in {5, 35} else primary
        add(
            "student_school_relationship",
            f"sf4:enrollment:{index:03d}",
            {
                "student_source_key": student_key,
                "student_number": f"SF4-S{index:03d}",
                "enrollment_status": "enrolled",
                "residence_mode": "day" if index >= 53 else "boarding",
                "primary_campus_source_key": primary,
                "administrative_campus_source_key": administrative,
            },
        )

    for index, person_key in enumerate(staff_keys, 1):
        add(
            "login_mapping",
            f"sf4:login:staff:{index:03d}",
            {
                "provider_code": "sf4_synthetic",
                "provider_tenant_key": "sf4-xinglan.invalid",
                "external_subject": f"fictional-staff-{index:03d}",
                "person_source_key": person_key,
                "status": "confirmed",
            },
        )

    organizations = (
        (
            "sf4:org:school-office",
            None,
            "school_wide",
            "SF4-O01",
            "校务办公室",
            "全校/校务办公室",
        ),
        (
            "sf4:org:east-teaching",
            "sf4:org:school-office",
            "campus",
            "SF4-O02",
            "东澜教学组",
            "全校/东澜校区/教学组",
        ),
        (
            "sf4:org:west-teaching",
            "sf4:org:school-office",
            "campus",
            "SF4-O03",
            "西汀教学组",
            "全校/西汀校区/教学组",
        ),
        (
            "sf4:org:east-dorm",
            "sf4:org:school-office",
            "campus",
            "SF4-O04",
            "东澜住宿组",
            "全校/东澜校区/住宿组",
        ),
        (
            "sf4:org:west-dorm",
            "sf4:org:school-office",
            "campus",
            "SF4-O05",
            "西汀住宿组",
            "全校/西汀校区/住宿组",
        ),
    )
    for key, parent, kind, code, name, path in organizations:
        add(
            "organization",
            key,
            {
                "parent_source_key": parent,
                "kind": kind,
                "business_code": code,
                "display_name": name,
                "short_name": name,
                "display_path": path,
                "status": "active",
            },
        )

    organization_links = (
        ("sf4:orgcamp:school-office:east", "sf4:org:school-office", campus_keys[0]),
        ("sf4:orgcamp:school-office:west", "sf4:org:school-office", campus_keys[1]),
        ("sf4:orgcamp:east-teaching:east", "sf4:org:east-teaching", campus_keys[0]),
        ("sf4:orgcamp:west-teaching:west", "sf4:org:west-teaching", campus_keys[1]),
        ("sf4:orgcamp:east-dorm:east", "sf4:org:east-dorm", campus_keys[0]),
        ("sf4:orgcamp:west-dorm:west", "sf4:org:west-dorm", campus_keys[1]),
    )
    for key, organization_key, campus_key in organization_links:
        add(
            "organization_campus_relationship",
            key,
            {
                "organization_source_key": organization_key,
                "campus_source_key": campus_key,
            },
        )

    for index, person_key in enumerate(staff_keys, 1):
        organization_key = (
            "sf4:org:east-teaching" if index <= 12 else "sf4:org:west-teaching"
        )
        content: dict[str, Any] = {
            "person_source_key": person_key,
            "organization_source_key": organization_key,
            "position_title": f"虚构岗位{index:02d}",
            "semantic_role_code": "staff_member",
            "assignment_kind": "substantive",
            "appointment_source": "sf4_synthetic_record",
            "approval_reference": f"SF4-APP-{index:03d}",
            "status": "confirmed",
        }
        if index == 4:
            content["valid_to"] = _dt(2027, 6, 30)
        add("assignment", f"sf4:assignment:main:{index:03d}", content)
    add(
        "assignment",
        "sf4:assignment:staff001:parttime",
        {
            "person_source_key": staff_keys[0],
            "organization_source_key": "sf4:org:west-teaching",
            "position_title": "跨组织虚构兼职",
            "semantic_role_code": "support_member",
            "assignment_kind": "part_time",
            "appointment_source": "sf4_synthetic_record",
            "approval_reference": "SF4-APP-PT-001",
            "status": "confirmed",
        },
    )
    add(
        "assignment",
        "sf4:assignment:staff002:acting",
        {
            "person_source_key": staff_keys[1],
            "organization_source_key": "sf4:org:east-dorm",
            "position_title": "临时代任（虚构）",
            "semantic_role_code": "acting_member",
            "assignment_kind": "acting",
            "appointment_source": "sf4_synthetic_record",
            "approval_reference": "SF4-APP-ACT-002",
            "status": "confirmed",
            "valid_to": _dt(2026, 9, 30),
        },
    )
    add(
        "assignment",
        "sf4:assignment:staff003:history",
        {
            "person_source_key": staff_keys[2],
            "organization_source_key": "sf4:org:west-dorm",
            "position_title": "历史虚构任职",
            "semantic_role_code": "former_member",
            "assignment_kind": "substantive",
            "appointment_source": "sf4_synthetic_record",
            "approval_reference": "SF4-APP-HIS-003",
            "status": "confirmed",
            "valid_to": _dt(2025, 7, 1),
        },
        effective_at=_dt(2024, 9, 1),
    )
    change(
        "revoke",
        "assignment",
        "sf4:assignment:main:004",
        effective_at=_dt(2026, 8, 10),
    )

    locations: list[tuple[str, str | None, str, str, str, str, tuple[str, ...]]] = [
        (campus_keys[0], None, "campus", "SF4-L-E", "东澜校区", "星澜/东澜校区", ()),
        (campus_keys[1], None, "campus", "SF4-L-W", "西汀校区", "星澜/西汀校区", ()),
    ]
    building_keys = ("sf4:location:dorm:east", "sf4:location:dorm:west")
    locations.extend(
        (
            (
                building_keys[0],
                campus_keys[0],
                "building",
                "SF4-D-E",
                "东澜宿舍一号楼",
                "星澜/东澜校区/宿舍一号楼",
                ("东一舍",),
            ),
            (
                building_keys[1],
                campus_keys[1],
                "building",
                "SF4-D-W",
                "西汀宿舍二号楼",
                "星澜/西汀校区/宿舍二号楼",
                ("西二舍",),
            ),
        )
    )
    floor_keys: list[str] = []
    room_keys: list[str] = []
    for campus_label, building_key in (
        ("east", building_keys[0]),
        ("west", building_keys[1]),
    ):
        for floor in range(1, 3):
            floor_key = f"sf4:location:floor:{campus_label}:{floor}"
            floor_keys.append(floor_key)
            locations.append(
                (
                    floor_key,
                    building_key,
                    "floor",
                    f"SF4-F-{campus_label[0].upper()}{floor}",
                    f"{floor}层",
                    f"星澜/{'东澜' if campus_label == 'east' else '西汀'}校区/宿舍/{floor}层",
                    (),
                )
            )
            for room_offset in range(1, 4):
                room_number = floor * 100 + room_offset
                room_key = f"sf4:location:room:{campus_label}:{room_number}"
                room_keys.append(room_key)
                locations.append(
                    (
                        room_key,
                        floor_key,
                        "dorm_room",
                        f"SF4-R-{campus_label[0].upper()}{room_number}",
                        f"{room_number}室",
                        (
                            f"星澜/{'东澜' if campus_label == 'east' else '西汀'}校区/"
                            f"宿舍/{floor}层/{room_number}室"
                        ),
                        (),
                    )
                )
    locations.extend(
        (
            (
                "sf4:location:learning-center",
                campus_keys[0],
                "facility",
                "SF4-LC",
                "旧学习中心",
                "星澜/东澜校区/旧学习中心",
                ("共享空间",),
            ),
            (
                "sf4:location:common-room-a",
                campus_keys[0],
                "facility",
                "SF4-CA",
                "东澜公共活动室",
                "星澜/东澜校区/公共活动室",
                ("活动室",),
            ),
            (
                "sf4:location:common-room-b",
                campus_keys[1],
                "facility",
                "SF4-CB",
                "西汀公共活动室",
                "星澜/西汀校区/公共活动室",
                ("活动室",),
            ),
            (
                "sf4:location:retired-store",
                campus_keys[1],
                "facility",
                "SF4-RS",
                "已停用旧储物间",
                "星澜/西汀校区/旧储物间",
                (),
            ),
        )
    )
    for key, parent, kind, code, name, path, aliases in locations:
        location_content: dict[str, Any] = {
            "parent_source_key": parent,
            "kind": kind,
            "business_code": code,
            "display_name": name,
            "display_path": path,
            "status": "active",
        }
        if aliases:
            location_content["aliases"] = aliases
        add(
            "location",
            key,
            location_content,
        )
    change(
        "upsert",
        "location",
        "sf4:location:learning-center",
        effective_at=_dt(2026, 8, 15),
        content={
            "parent_source_key": campus_keys[0],
            "kind": "facility",
            "business_code": "SF4-LC",
            "display_name": "星澜学习中心",
            "display_path": "星澜/东澜校区/星澜学习中心",
            "aliases": ("旧学习中心", "共享空间"),
            "status": "active",
        },
    )
    change(
        "end",
        "location",
        "sf4:location:retired-store",
        effective_at=_dt(2026, 8, 5),
    )
    for index, (location_key, alias, alias_kind) in enumerate(
        (
            ("sf4:location:learning-center", "旧学习中心", "former"),
            ("sf4:location:common-room-a", "活动室", "common"),
            ("sf4:location:common-room-b", "活动室", "common"),
        ),
        1,
    ):
        add(
            "location_alias",
            f"sf4:location-alias:{index:03d}",
            {
                "location_source_key": location_key,
                "alias": alias,
                "alias_kind": alias_kind,
            },
        )

    for index, room_key in enumerate(room_keys, 1):
        room_content: dict[str, Any] = {
            "room_source_key": room_key,
            "authorized_bed_capacity": 5,
            "accommodation_zone": "synthetic_boarding",
            "is_available": True,
        }
        if index == 1:
            room_content["accessibility_tags"] = ("test_only",)
        add(
            "dorm_room",
            f"sf4:dorm-room:{index:02d}",
            room_content,
        )
    for index in range(1, 61):
        room_key = room_keys[(index - 1) // 5]
        add(
            "dorm_bed",
            f"sf4:bed:{index:02d}",
            {
                "room_source_key": room_key,
                "business_code": f"SF4-B{index:02d}",
                "display_name": f"{((index - 1) % 5) + 1}号床",
                "availability": "unavailable" if index == 60 else "available",
            },
        )

    residence_intervals: list[ResidenceInterval] = []
    current_beds = (31, *range(1, 30), *range(32, 50))
    for index, bed_number in enumerate(current_beds, 1):
        key = f"sf4:residence:current:{index:03d}"
        valid_from = _dt(2026, 1, 1) if index == 2 else _dt(2026, 8, 1)
        add(
            "student_bed_residence",
            key,
            {
                "student_source_key": student_keys[index - 1],
                "bed_source_key": f"sf4:bed:{bed_number:02d}",
            },
            effective_at=valid_from,
        )
        residence_intervals.append(
            ResidenceInterval(
                key,
                student_keys[index - 1],
                f"sf4:bed:{bed_number:02d}",
                valid_from,
                None,
            )
        )
    historical_specs = (
        (
            "sf4:residence:s001:a",
            student_keys[0],
            "sf4:bed:56",
            _dt(2025, 2, 1),
            _dt(2025, 7, 1),
        ),
        (
            "sf4:residence:s001:b",
            student_keys[0],
            "sf4:bed:57",
            _dt(2025, 7, 1),
            _dt(2026, 1, 15),
        ),
        (
            "sf4:residence:s002:same-bed",
            student_keys[1],
            "sf4:bed:01",
            _dt(2025, 9, 1),
            _dt(2026, 1, 1),
        ),
        (
            "sf4:residence:s051:bounded",
            student_keys[50],
            "sf4:bed:59",
            _dt(2025, 1, 10),
            _dt(2025, 6, 30),
        ),
    )
    for key, student_key, bed_key, valid_from, valid_to in historical_specs:
        add(
            "student_bed_residence",
            key,
            {
                "student_source_key": student_key,
                "bed_source_key": bed_key,
                "valid_to": valid_to,
            },
            effective_at=valid_from,
        )
        residence_intervals.append(
            ResidenceInterval(key, student_key, bed_key, valid_from, valid_to)
        )
    ended_key = "sf4:residence:s049:ended"
    add(
        "student_bed_residence",
        ended_key,
        {
            "student_source_key": student_keys[48],
            "bed_source_key": "sf4:bed:58",
        },
        effective_at=_dt(2026, 2, 1),
    )
    ended_at = _dt(2026, 8, 1)
    change(
        "end",
        "student_bed_residence",
        ended_key,
        effective_at=ended_at,
    )
    residence_intervals.append(
        ResidenceInterval(
            ended_key,
            student_keys[48],
            "sf4:bed:58",
            _dt(2026, 2, 1),
            None,
            terminated_at=ended_at,
        )
    )
    future_key = "sf4:residence:s050:future"
    future_at = _dt(2026, 9, 15)
    add(
        "student_bed_residence",
        future_key,
        {
            "student_source_key": student_keys[49],
            "bed_source_key": "sf4:bed:50",
        },
        effective_at=future_at,
    )
    residence_intervals.append(
        ResidenceInterval(
            future_key,
            student_keys[49],
            "sf4:bed:50",
            future_at,
            None,
        )
    )

    person_contacts = (
        (
            "sf4:person-contact:staff001",
            staff_keys[0],
            "email",
            "staff001@example.invalid",
            True,
        ),
        (
            "sf4:person-contact:staff023:old",
            staff_keys[22],
            "mobile",
            "000-2300-0001",
            False,
        ),
        (
            "sf4:person-contact:staff023:new",
            staff_keys[22],
            "mobile",
            "000-2300-0002",
            True,
        ),
        (
            "sf4:person-contact:parent001",
            contact_person_keys[0],
            "mobile",
            "000-3100-0001",
            True,
        ),
        (
            "sf4:person-contact:grandparent002",
            contact_person_keys[1],
            "mobile",
            "000-3200-0002",
            True,
        ),
        (
            "sf4:person-contact:parent003a",
            contact_person_keys[2],
            "mobile",
            "000-3300-0003",
            True,
        ),
        (
            "sf4:person-contact:parent003b",
            contact_person_keys[3],
            "mobile",
            "000-3400-0004",
            True,
        ),
    )
    for key, person_key, kind, value, primary in person_contacts:
        add(
            "person_contact",
            key,
            {
                "person_source_key": person_key,
                "contact_kind": kind,
                "contact_value": value,
                "is_primary": primary,
                "visibility_scope": "private",
                "consent_reference": "SF4-SYNTHETIC-CONSENT",
            },
            effective_at=_dt(2025, 6, 1) if key.endswith(":old") else BASE_AT,
        )
    change(
        "end",
        "person_contact",
        "sf4:person-contact:staff023:old",
        effective_at=_dt(2026, 6, 1),
    )
    student_contacts = (
        (
            "sf4:student-contact:s001:mother",
            student_keys[0],
            contact_person_keys[0],
            "mother",
            1,
            True,
        ),
        (
            "sf4:student-contact:s002:grandfather",
            student_keys[1],
            contact_person_keys[1],
            "grandfather",
            1,
            True,
        ),
        (
            "sf4:student-contact:s003:mother",
            student_keys[2],
            contact_person_keys[2],
            "mother",
            1,
            True,
        ),
        (
            "sf4:student-contact:s003:father",
            student_keys[2],
            contact_person_keys[3],
            "father",
            2,
            False,
        ),
    )
    for (
        key,
        student_key,
        person_key,
        relationship,
        priority,
        emergency,
    ) in student_contacts:
        add(
            "student_contact",
            key,
            {
                "student_source_key": student_key,
                "contact_person_source_key": person_key,
                "relationship_kind": relationship,
                "priority": priority,
                "is_emergency": emergency,
            },
        )
    add(
        "official_contact",
        "sf4:official-contact:school",
        {
            "owner_kind": "school",
            "contact_kind": "email",
            "contact_value": "office@xinglan.example.invalid",
            "service_hours": "虚构服务时段 08:00-17:00",
            "lifecycle_status": "active",
            "visibility_scope": "public",
        },
    )
    add(
        "official_contact",
        "sf4:official-contact:east-dorm",
        {
            "owner_kind": "organization",
            "organization_source_key": "sf4:org:east-dorm",
            "contact_kind": "phone",
            "contact_value": "000-4100-0001",
            "service_hours": "虚构值守时段 18:00-22:00",
            "lifecycle_status": "active",
            "visibility_scope": "school_authenticated",
        },
    )

    for index in range(1, 4):
        add("policy", f"sf4:policy:{index:02d}", {})
    policy_versions = (
        (
            "sf4:policy-version:01:v1",
            "sf4:policy:01",
            "SF4-POL-01-V1",
            "虚构寄宿生活指引（旧版）",
            "internal",
            _dt(2025, 1, 1),
            _dt(2025, 1, 10),
            _dt(2026, 1, 1),
            "虚构旧版制度正文。",
            None,
        ),
        (
            "sf4:policy-version:01:v2",
            "sf4:policy:01",
            "SF4-POL-01-V2",
            "虚构寄宿生活指引",
            "internal",
            _dt(2025, 12, 1),
            _dt(2026, 1, 1),
            _dt(2026, 10, 1),
            "虚构现行制度正文。",
            "sf4:policy-version:01:v1",
        ),
        (
            "sf4:policy-version:01:v3",
            "sf4:policy:01",
            "SF4-POL-01-V3",
            "虚构寄宿生活指引（未来版）",
            "internal",
            _dt(2026, 8, 1),
            _dt(2026, 10, 1),
            None,
            "虚构未来制度正文。",
            "sf4:policy-version:01:v2",
        ),
        (
            "sf4:policy-version:02:restricted",
            "sf4:policy:02",
            "SF4-POL-02-R",
            "虚构受限内部演练制度",
            "restricted",
            _dt(2026, 6, 1),
            _dt(2026, 6, 15),
            None,
            "FICTIONAL-RESTRICTED-SENTINEL-SF4：仅验证存储，不声明权限保护。",
            None,
        ),
        (
            "sf4:policy-version:03:revoked",
            "sf4:policy:03",
            "SF4-POL-03-RV",
            "虚构计划到期后撤销制度",
            "public",
            _dt(2026, 7, 1),
            _dt(2026, 7, 15),
            _dt(2027, 7, 15),
            "虚构撤销测试正文。",
            None,
        ),
    )
    for (
        key,
        policy_key,
        code,
        name,
        classification,
        published_at,
        effective_from,
        effective_to,
        text,
        supersedes,
    ) in policy_versions:
        add(
            "policy_version",
            key,
            {
                "policy_source_key": policy_key,
                "business_code": code,
                "display_name": name,
                "category_code": "synthetic_policy",
                "classification": classification,
                "publishing_organization_source_key": "sf4:org:school-office",
                "published_at": published_at,
                "effective_from": effective_from,
                "effective_to": effective_to,
                "content_text": text,
                "version_note": "SF4 人工合成测试版本",
                "supersedes_policy_version_source_key": supersedes,
            },
            effective_at=effective_from,
        )
    change(
        "revoke",
        "policy_version",
        "sf4:policy-version:03:revoked",
        effective_at=_dt(2026, 8, 10),
    )
    add(
        "policy_scope",
        "sf4:policy-scope:01:east",
        {
            "policy_source_key": "sf4:policy:01",
            "campus_source_key": campus_keys[0],
        },
    )
    add(
        "policy_scope",
        "sf4:policy-scope:02:west-dorm",
        {
            "policy_source_key": "sf4:policy:02",
            "organization_source_key": "sf4:org:west-dorm",
        },
    )

    scenario_evidence = {
        1: "staff001 主岗加跨组织兼职；staff002 临时代任；staff003 有结束时间的历史任职。",
        2: "staff011/staff012 同名“模拟重名”；staff024 无联系方式；"
        "staff023 新旧联系方式并结束旧值。",
        3: "learning-center 从旧学习中心更名；两个地点共享“活动室”别名；retired-store 已结束。",
        4: "当前未使用床位存在，sf4:bed:60 的 availability=unavailable。",
        5: "sf4:residence:s050:future 在判定时点之后生效。",
        6: "student001 具有 A→B→C 三段床位；student049 已退宿。",
        7: "student002 前后两段都指向 sf4:bed:01，是同床技术切片。",
        8: "sf4:residence:s051:bounded 的 valid_from 与 valid_to 均明确。",
        9: "student053–060 为走读；每条在校关系都保存主要和行政校区。",
        10: "student001 主要校区为 east，当前床位 sf4:bed:31 位于 west。",
        11: "student001 只登记母亲；student002 登记外祖父；不记录家庭结构推断。",
        12: "policy01 多版本；policy02 restricted 正文含虚构哨兵。",
        13: "assignment main:004 与 policy-version:03 在原计划结束前 revoke，原到期字段保留。",
        14: "school-office 显式关联两校区；四个校区专属组织各只关联本校区。",
        15: "policy01 仅 east 校区；policy02 仅 west-dorm 组织。",
        16: "两校区均有组织、教职工、学生和宿舍；school-office 同时显式关联两校区。",
    }
    return SimulatedSchoolGroundTruth(
        as_of=AS_OF,
        base_changes=_finalize(base),
        lifecycle_changes=_finalize(lifecycle),
        residence_intervals=tuple(residence_intervals),
        scenario_evidence=scenario_evidence,
        staff_keys=staff_keys,
        student_keys=student_keys,
        campus_keys=campus_keys,
        dorm_building_keys=building_keys,
        dorm_floor_keys=tuple(floor_keys),
        dorm_room_keys=tuple(room_keys),
    )


def _normalize_xlsx(content: bytes) -> bytes:
    source = BytesIO(content)
    output = BytesIO()
    with (
        zipfile.ZipFile(source, "r") as archive,
        zipfile.ZipFile(
            output,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as normalized,
    ):
        for name in sorted(archive.namelist()):
            info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            payload = archive.read(name)
            if name == "docProps/core.xml":
                payload = re.sub(
                    rb"<dcterms:modified[^>]*>[^<]+</dcterms:modified>",
                    (
                        b'<dcterms:modified xsi:type="dcterms:W3CDTF">'
                        b"2026-08-22T12:00:00Z</dcterms:modified>"
                    ),
                    payload,
                )
            normalized.writestr(info, payload)
    return output.getvalue()


def _policy_status(change: CanonicalFactChangeV1) -> str:
    assert change.content is not None
    effective_from = change.content.effective_from
    effective_to = change.content.effective_to
    if effective_from is not None and effective_from > AS_OF:
        return "scheduled"
    if effective_to is not None and effective_to <= AS_OF:
        return "expired"
    return "effective"


def _render_workbook(
    changes: tuple[CanonicalFactChangeV1, ...], *, phase: str
) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.properties.creator = "SF4 deterministic synthetic generator"
    workbook.properties.title = f"SF4 fictional boarding school {phase}"
    workbook.properties.created = AS_OF.replace(tzinfo=None)
    workbook.properties.modified = AS_OF.replace(tzinfo=None)
    by_kind: dict[str, list[CanonicalFactChangeV1]] = {}
    for change in changes:
        by_kind.setdefault(change.fact_kind, []).append(change)
    for sheet_name, spec in SCHOOL_FACT_SHEET_SPECS.items():
        sheet = workbook.create_sheet(sheet_name)
        sheet.sheet_view.showGridLines = False
        sheet.append(spec.headers)
        for cell in sheet[1]:
            cell.fill = PatternFill("solid", fgColor="1F4E78")
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:{sheet.cell(1, len(spec.headers)).coordinate}"
        if spec.fact_kind is None:
            sheet.append(
                (
                    SCHOOL_FACT_TEMPLATE_VERSION,
                    f"SF4 人工合成测试数据；阶段={phase}；判定时点={AS_OF.isoformat()}",
                )
            )
        else:
            for change in by_kind.get(spec.fact_kind, []):
                payload = safe_content_payload(change.content) or {}
                row: dict[str, Any] = {
                    "操作": change.operation,
                    "稳定标识": change.source_key,
                    "预期版本": change.expected_version,
                    "生效时间": change.effective_at.isoformat(),
                }
                for header, field_code in spec.content_headers:
                    value = (
                        _policy_status(change)
                        if field_code == "__policy_status__"
                        and change.content is not None
                        else payload.get(field_code)
                    )
                    if field_code == "is_primary" and isinstance(value, bool):
                        value = "主要" if value else "备用"
                    if isinstance(value, datetime):
                        value = value.isoformat()
                    elif isinstance(value, (tuple, list)):
                        value = ",".join(str(item) for item in value)
                    row[header] = value
                sheet.append(tuple(row.get(header) for header in spec.headers))
            operation_validation = DataValidation(
                type="list",
                formula1='"' + ",".join(spec.allowed_operations) + '"',
                allow_blank=False,
            )
            sheet.add_data_validation(operation_validation)
            operation_validation.add("A2:A10001")
        widths = {
            1: 14,
            2: 38,
            3: 12,
            4: 28,
        }
        for column_index in range(1, len(spec.headers) + 1):
            sheet.column_dimensions[
                sheet.cell(1, column_index).column_letter
            ].width = widths.get(column_index, 24)
        sheet.row_dimensions[1].height = 24
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return _normalize_xlsx(buffer.getvalue())


def render_assets(
    ground_truth: SimulatedSchoolGroundTruth,
) -> dict[Path, bytes]:
    return {
        BASE_WORKBOOK_PATH: _render_workbook(ground_truth.base_changes, phase="base"),
        LIFECYCLE_WORKBOOK_PATH: _render_workbook(
            ground_truth.lifecycle_changes, phase="lifecycle"
        ),
    }


def _write_or_check(*, check: bool) -> int:
    ground_truth = build_ground_truth()
    frozen_ground_truth = ground_truth.render_human_ground_truth()
    if (
        not GROUND_TRUTH_PATH.is_file()
        or GROUND_TRUTH_PATH.read_text(encoding="utf-8") != frozen_ground_truth
    ):
        print(
            "SF4 frozen ground truth differs; review and edit it explicitly before "
            "regenerating workbooks: "
            f"{GROUND_TRUTH_PATH.relative_to(REPO_ROOT).as_posix()}",
            file=sys.stderr,
        )
        return 1
    rendered = render_assets(ground_truth)
    if check:
        mismatches = []
        for path, expected in rendered.items():
            if not path.is_file() or path.read_bytes() != expected:
                mismatches.append(path.relative_to(REPO_ROOT).as_posix())
        if mismatches:
            print("SF4 assets differ: " + ", ".join(mismatches), file=sys.stderr)
            return 1
        print(
            "SF4 assets deterministic: "
            f"{len(ground_truth.base_changes)} base changes, "
            f"{len(ground_truth.lifecycle_changes)} lifecycle changes, "
            f"{ground_truth.current_resident_count()} current residents."
        )
        return 0
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for path, content in rendered.items():
        path.write_bytes(content)
        print(path.relative_to(REPO_ROOT).as_posix())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    return _write_or_check(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
