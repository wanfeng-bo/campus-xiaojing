from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from hashlib import sha256

from app.platform.school_facts.contracts import MAX_SCHOOL_FACT_CHANGES

MAX_SCHOOL_FACT_STATUS_DECISIONS = MAX_SCHOOL_FACT_CHANGES * 2

_STATUS_DISPLAY_PUNCTUATION = frozenset(" _-().（）·./")


def require_safe_status_original_value(value: str) -> str:
    """确认前可见的学校原写法；只允许短、无控制符的状态标签。"""

    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 64
        or any(
            not (
                unicodedata.category(character)[0] in {"L", "N"}
                or character in _STATUS_DISPLAY_PUNCTUATION
            )
            for character in value
        )
    ):
        raise ValueError("状态原写法不是安全的短标签。")
    return value


@dataclass(frozen=True, slots=True)
class StatusMappingEntry:
    sheet_code: str
    field_code: str
    school_value: str
    standard_value: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value and value == value.strip()
            for value in (
                self.sheet_code,
                self.field_code,
                self.school_value,
                self.standard_value,
            )
        ):
            raise ValueError("状态映射条目必须使用已去除首尾空白的非空文本。")
        require_safe_status_original_value(self.school_value)


@dataclass(frozen=True, slots=True)
class StatusMappingProfile:
    version: str
    entries: tuple[StatusMappingEntry, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version, str)
            or not self.version
            or self.version != self.version.strip()
        ):
            raise ValueError("状态映射版本无效。")
        if not self.entries:
            raise ValueError("状态映射配置不能为空。")


@dataclass(frozen=True, slots=True)
class StatusMappingResult:
    original_value: str
    standard_value: str
    mapping_version: str
    sheet_code: str
    field_code: str
    row_number: int | None = None
    source_slot: str | None = None

    def __post_init__(self) -> None:
        require_safe_status_original_value(self.original_value)


class StatusMappingError(ValueError):
    """对外只发布固定错误码，不把来源单元格原文放进异常。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _entries(
    sheet: str,
    field: str,
    groups: dict[str, tuple[str, ...]],
) -> tuple[StatusMappingEntry, ...]:
    return tuple(
        StatusMappingEntry(sheet, field, school_value, standard)
        for standard, school_values in groups.items()
        for school_value in (standard, *school_values)
    )


_COMMON_ACTIVE = {
    "active": ("正常", "启用", "在用", "有效"),
    "inactive": ("停用", "关闭", "不再使用"),
}

DEFAULT_STATUS_MAPPING_PROFILE = StatusMappingProfile(
    version="school_statuses_v1",
    entries=(
        *_entries("01_学校", "lifecycle_status", _COMMON_ACTIVE),
        *_entries(
            "02_人员",
            "lifecycle_status",
            {
                "active": ("在职", "在岗", "现职"),
                "inactive": ("离职", "退休", "调出"),
            },
        ),
        *_entries(
            "03_学生",
            "status",
            {"active": ("正常", "启用"), "inactive": ("停用",), "merged": ("已合并",)},
        ),
        *_entries(
            "04_学生在校状态",
            "enrollment_status",
            {
                "enrolled": ("在读", "在籍"),
                "suspended": ("休学",),
                "transferred_out": ("转出", "转学离校"),
                "withdrawn": ("退学", "辍学离校"),
                "graduated": ("毕业", "结业离校"),
            },
        ),
        *_entries(
            "04_学生在校状态",
            "residence_mode",
            {"day": ("走读", "通校", "不住校"), "boarding": ("寄宿", "住宿", "住校")},
        ),
        *_entries(
            "05_登录映射",
            "status",
            {"draft": ("待确认",), "confirmed": ("已确认", "有效"), "rejected": ("已拒绝", "失效")},
        ),
        *_entries("06_组织", "status", _COMMON_ACTIVE),
        *_entries(
            "07_任职",
            "assignment_kind",
            {
                "substantive": ("主岗", "正式", "专任"),
                "part_time": ("兼任", "兼职", "兼岗"),
                "acting": ("代岗", "代理", "临时负责", "顶岗"),
            },
        ),
        *_entries(
            "07_任职",
            "status",
            {"draft": ("待确认",), "confirmed": ("已确认", "有效"), "rejected": ("已拒绝", "撤销")},
        ),
        *_entries("08_地点", "status", _COMMON_ACTIVE),
        *_entries(
            "11_宿舍床位",
            "availability",
            {
                "available": ("可用", "可分配", "空床"),
                "unavailable": ("维修", "封床", "暂停使用"),
                "inactive": ("拆除", "永久停用"),
            },
        ),
        *_entries(
            "13_个人联系方式",
            "contact_priority",
            {"primary": ("主要", "首选", "默认号码"), "secondary": ("备用", "次要")},
        ),
        *_entries("15_官方联系方式", "lifecycle_status", _COMMON_ACTIVE),
        *_entries(
            "17_制度版本",
            "policy_status",
            {
                "effective": ("现行", "执行中", "有效"),
                "scheduled": ("待生效", "未启用"),
                "expired": ("废止", "失效", "已替代"),
            },
        ),
        *_entries(
            "17_制度版本",
            "classification",
            {
                "public": ("公开",),
                "internal": ("校内", "内部"),
                "restricted": ("保密", "受限", "仅指定人员"),
            },
        ),
    ),
)


def map_school_status(
    *,
    sheet_code: str,
    field_code: str,
    raw_value: object,
    profile: StatusMappingProfile = DEFAULT_STATUS_MAPPING_PROFILE,
    row_number: int | None = None,
) -> StatusMappingResult:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise StatusMappingError("STATUS_MAPPING_REQUIRED")
    normalized = raw_value.strip()
    candidates = {
        entry.standard_value
        for entry in profile.entries
        if entry.sheet_code == sheet_code
        and entry.field_code == field_code
        and entry.school_value == normalized
    }
    if not candidates:
        raise StatusMappingError("STATUS_MAPPING_REQUIRED")
    if len(candidates) != 1:
        raise StatusMappingError("STATUS_MAPPING_AMBIGUOUS")
    return StatusMappingResult(
        original_value=normalized,
        standard_value=next(iter(candidates)),
        mapping_version=profile.version,
        sheet_code=sheet_code,
        field_code=field_code,
        row_number=row_number,
    )


def status_mapping_profile_sha256(profile: StatusMappingProfile) -> str:
    """对完整真值表做规范摘要；同版本不同配置不会被误当成同一映射。"""

    payload = {
        "version": profile.version,
        "entries": sorted(
            (
                entry.sheet_code,
                entry.field_code,
                entry.school_value,
                entry.standard_value,
            )
            for entry in profile.entries
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


__all__ = [
    "DEFAULT_STATUS_MAPPING_PROFILE",
    "StatusMappingEntry",
    "StatusMappingError",
    "StatusMappingProfile",
    "StatusMappingResult",
    "map_school_status",
    "status_mapping_profile_sha256",
]
