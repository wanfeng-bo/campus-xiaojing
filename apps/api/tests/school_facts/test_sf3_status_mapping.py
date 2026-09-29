from __future__ import annotations

import pytest

from app.platform.master_data_imports.status_mappings import (
    DEFAULT_STATUS_MAPPING_PROFILE,
    StatusMappingEntry,
    StatusMappingError,
    StatusMappingProfile,
    map_school_status,
)


@pytest.mark.parametrize(
    ("sheet_code", "field_code", "raw_value", "expected"),
    [
        ("02_人员", "lifecycle_status", "在职", "active"),
        ("07_任职", "assignment_kind", "兼岗", "part_time"),
        ("04_学生在校状态", "enrollment_status", "休学", "suspended"),
        ("04_学生在校状态", "residence_mode", "住校", "boarding"),
        ("11_宿舍床位", "availability", "维修", "unavailable"),
        ("13_个人联系方式", "contact_priority", "首选", "primary"),
        ("15_官方联系方式", "lifecycle_status", "停用", "inactive"),
        ("17_制度版本", "policy_status", "待生效", "scheduled"),
        ("17_制度版本", "classification", "校内", "internal"),
    ],
)
def test_default_status_profile_is_context_scoped_and_deterministic(
    sheet_code: str,
    field_code: str,
    raw_value: str,
    expected: str,
) -> None:
    result = map_school_status(
        sheet_code=sheet_code,
        field_code=field_code,
        raw_value=raw_value,
    )

    assert result.standard_value == expected
    assert result.original_value == raw_value
    assert result.mapping_version == DEFAULT_STATUS_MAPPING_PROFILE.version


def test_unknown_status_fails_closed_without_fuzzy_guessing() -> None:
    with pytest.raises(StatusMappingError) as caught:
        map_school_status(
            sheet_code="04_学生在校状态",
            field_code="enrollment_status",
            raw_value="大概在读",
        )

    assert caught.value.code == "STATUS_MAPPING_REQUIRED"
    assert "大概在读" not in str(caught.value)


def test_same_word_is_not_reused_outside_its_registered_context() -> None:
    with pytest.raises(StatusMappingError) as caught:
        map_school_status(
            sheet_code="11_宿舍床位",
            field_code="availability",
            raw_value="在职",
        )

    assert caught.value.code == "STATUS_MAPPING_REQUIRED"


def test_ambiguous_profile_value_fails_closed() -> None:
    profile = StatusMappingProfile(
        version="school_statuses_test_v1",
        entries=(
            StatusMappingEntry("02_人员", "lifecycle_status", "正常", "active"),
            StatusMappingEntry("02_人员", "lifecycle_status", "正常", "inactive"),
        ),
    )

    with pytest.raises(StatusMappingError) as caught:
        map_school_status(
            sheet_code="02_人员",
            field_code="lifecycle_status",
            raw_value="正常",
            profile=profile,
        )

    assert caught.value.code == "STATUS_MAPPING_AMBIGUOUS"


def test_mapping_version_reproduces_the_exact_same_decision() -> None:
    first = map_school_status(
        sheet_code="07_任职",
        field_code="assignment_kind",
        raw_value="代岗",
    )
    second = map_school_status(
        sheet_code="07_任职",
        field_code="assignment_kind",
        raw_value="代岗",
        profile=DEFAULT_STATUS_MAPPING_PROFILE,
    )

    assert first == second


@pytest.mark.parametrize(
    "unsafe_value",
    (
        "启用\n管理员",
        "<启用>",
        "启用🙂",
        "a" * 65,
    ),
)
def test_status_original_value_is_bounded_display_text(unsafe_value: str) -> None:
    with pytest.raises(ValueError, match="状态原写法"):
        StatusMappingEntry(
            "01_学校",
            "lifecycle_status",
            unsafe_value,
            "active",
        )
