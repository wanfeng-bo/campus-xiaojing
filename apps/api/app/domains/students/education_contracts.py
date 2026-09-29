"""Finite F4 education content vocabulary; no executable or arbitrary query fields."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, cast
from zoneinfo import ZoneInfo

from pydantic import Field, StringConstraints

from .teaching_contracts import (
    TEACHING_ARRAY_REFERENCES,
    TEACHING_FIELDS,
    TEACHING_KINDS,
    TEACHING_REFERENCES,
    TeachingContentFields,
    validate_teaching_content,
)

if TYPE_CHECKING:
    from app.platform.school_facts.contracts import CanonicalFactContentV3

SourceKey = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z0-9][a-z0-9._:-]{2,254}$")]
Text = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1000)
]


class EducationContentFields(TeachingContentFields):
    stage_source_key: SourceKey | None = None
    grade_source_key: SourceKey | None = None
    calendar_source_key: SourceKey | None = None
    cohort_source_key: SourceKey | None = None
    class_source_key: SourceKey | None = None
    predecessor_class_source_key: SourceKey | None = None
    position_source_key: SourceKey | None = None
    school_relationship_source_key: SourceKey | None = None
    education_target_source_key: SourceKey | None = None
    education_target_kind: str | None = None
    grade_source_keys: tuple[SourceKey, ...] | None = Field(
        default=None, min_length=1, max_length=20
    )
    grade_order: int | None = Field(default=None, strict=True, ge=1, le=20)
    start_cycle: int | None = Field(default=None, strict=True, ge=1900, le=9998)
    end_cycle: int | None = Field(default=None, strict=True, ge=1901, le=9999)
    academic_year: int | None = Field(default=None, strict=True, ge=1900, le=9998)
    month: int | None = Field(default=None, strict=True, ge=1, le=12)
    day: int | None = Field(default=None, strict=True, ge=1, le=31)
    class_number: int | None = Field(default=None, strict=True, ge=1, le=9999)
    cohort_kind: (
        Literal["original_intake", "school_transfer_intake", "expected_graduation"] | None
    ) = None
    membership_status: Literal["assigned", "unassigned"] | None = None
    teacher_role: Literal["primary", "deputy", "joint", "acting"] | None = None
    duty_scope: Text | None = None
    location_purpose: Literal["main_classroom", "other_fixed"] | None = None
    lineage_kind: Literal["regrouped_from", "split_from", "merged_from"] | None = None
    allow_multiple: bool | None = Field(default=None, strict=True)
    allow_concurrent: bool | None = Field(default=None, strict=True)
    max_holders: int | None = Field(default=None, strict=True, ge=1, le=100)
    teacher_role_limits: (
        dict[
            Literal["primary", "deputy", "joint", "acting"],
            Annotated[int, Field(strict=True, ge=1, le=100)],
        ]
        | None
    ) = None
    teacher_mode: Literal["single_primary", "joint"] | None = None
    primary_classroom_limit: int | None = Field(default=None, strict=True, ge=1, le=100)
    acting_for_role: Literal["primary", "deputy", "joint"] | None = None
    field_name: Text | None = None


def _fields(required: str, optional: str = "") -> tuple[frozenset[str], frozenset[str]]:
    mandatory = frozenset(required.split())
    return mandatory | frozenset(optional.split()) | {"valid_to", "basis_reference"}, mandatory


F4_EDUCATION_FIELDS = {
    "education_stage": _fields("display_name", "business_code"),
    "education_offering": _fields("stage_source_key", "campus_source_key"),
    "education_grade": _fields("display_name stage_source_key grade_order", "business_code"),
    "education_calendar_rule": _fields(
        "display_name stage_source_key start_cycle month day timezone_name grade_source_keys",
        "end_cycle campus_source_key",
    ),
    "education_term": _fields("display_name calendar_source_key academic_year valid_to"),
    "education_grade_group": _fields(
        "calendar_source_key academic_year grade_source_key",
        "display_name organization_source_key campus_source_key",
    ),
    "education_cohort": _fields(
        "display_name stage_source_key cohort_kind", "academic_year business_code"
    ),
    "student_cohort_membership": _fields("student_source_key cohort_source_key"),
    "education_class": _fields(
        "display_name",
        "business_code short_name organization_source_key teacher_role_limits "
        "teacher_mode primary_classroom_limit",
    ),
    "class_placement": _fields(
        "class_source_key calendar_source_key grade_source_key campus_source_key", "class_number"
    ),
    "class_lineage": _fields("class_source_key predecessor_class_source_key lineage_kind"),
    "student_grade_placement": _fields(
        "student_source_key calendar_source_key", "grade_source_key"
    ),
    "student_class_membership": _fields(
        "student_source_key school_relationship_source_key membership_status", "class_source_key"
    ),
    "class_teacher_assignment": _fields(
        "class_source_key assignment_source_key teacher_role", "duty_scope acting_for_role"
    ),
    "class_location": _fields("class_source_key location_source_key location_purpose"),
    "class_position_definition": _fields(
        "class_source_key display_name allow_multiple allow_concurrent max_holders", "business_code"
    ),
    "student_class_position": _fields("class_source_key student_source_key position_source_key"),
}
F4_EDUCATION_KINDS = frozenset(F4_EDUCATION_FIELDS)
F4_EDUCATION_CONTENT_FIELDS = {kind: allowed for kind, (allowed, _) in F4_EDUCATION_FIELDS.items()}
EDUCATION_FIELDS = {**F4_EDUCATION_FIELDS, **TEACHING_FIELDS}
EDUCATION_KINDS = frozenset(EDUCATION_FIELDS)
EDUCATION_FACT_KINDS = EDUCATION_KINDS
EDUCATION_CONTENT_FIELDS = {
    kind: allowed for kind, (allowed, _required) in EDUCATION_FIELDS.items()
}
EDUCATION_REFERENCES = {
    **TEACHING_REFERENCES,
    "stage_source_key": "education_stage",
    "grade_source_key": "education_grade",
    "calendar_source_key": "education_calendar_rule",
    "cohort_source_key": "education_cohort",
    "class_source_key": "education_class",
    "predecessor_class_source_key": "education_class",
    "position_source_key": "class_position_definition",
    "school_relationship_source_key": "student_school_relationship",
}


def education_extra_references(kind: str, content: object) -> tuple[tuple[str, str, str], ...]:
    """Finite dynamically typed references; indexed codes preserve mixed existing/new lists."""
    result: list[tuple[str, str, str]] = []
    for index, key in enumerate(getattr(content, "grade_source_keys", None) or ()):
        result.append((f"grade_source_keys.{index}", "education_grade", str(key)))
    for field, target_kind in TEACHING_ARRAY_REFERENCES.items():
        for index, key in enumerate(getattr(content, field, None) or ()):
            result.append((f"{field}.{index}", target_kind, str(key)))
    target = getattr(content, "education_target_source_key", None)
    target_kind = getattr(content, "education_target_kind", None)
    if target is not None and target_kind in EDUCATION_KINDS:
        result.append(("education_target_source_key", str(target_kind), str(target)))
    if kind == "fact_quality" and getattr(content, "target_fact_kind", None) in EDUCATION_KINDS:
        for field in ("target_source_key", "replacement_source_key"):
            # Historical correction targets are deliberately resolved as retained facts by Writer.
            if (
                field == "target_source_key"
                and getattr(content, "quality_kind", None) == "correction"
            ):
                continue
            key = getattr(content, field, None)
            if key is not None:
                result.append((field, str(getattr(content, "target_fact_kind", None)), str(key)))
    return tuple(result)


def validate_education_content(kind: str, content: object) -> None:
    c = cast("CanonicalFactContentV3", content)
    allowed, required = EDUCATION_FIELDS[kind]
    present = frozenset(c.model_fields_set)
    if (
        not present <= allowed
        or not required <= present
        or any(getattr(c, x) is None for x in required)
    ):
        raise ValueError("教育事实字段缺失或不属于本类别。")
    if kind in TEACHING_KINDS:
        validate_teaching_content(kind, c)
    if kind == "education_calendar_rule":
        import calendar

        assert c.timezone_name is not None and c.month is not None and c.day is not None
        assert c.start_cycle is not None and c.grade_source_keys is not None
        ZoneInfo(c.timezone_name)
        if c.day > calendar.monthrange(2000, c.month)[1] or (c.month, c.day) == (2, 29):
            raise ValueError("每年学年边界必须是实际存在的固定日期。")
        if c.end_cycle is not None and c.end_cycle <= c.start_cycle:
            raise ValueError("学年规则截止周期必须晚于开始周期。")
        if len(set(c.grade_source_keys)) != len(c.grade_source_keys):
            raise ValueError("年级递进序列不能重复。")
    if kind == "student_class_membership":
        if (c.membership_status == "assigned") != (c.class_source_key is not None):
            raise ValueError("正式归班须有班级，待分班不能携带班级。")
    if kind == "class_lineage" and c.class_source_key == c.predecessor_class_source_key:
        raise ValueError("新班不能以自身作为原班。")
    if kind == "class_position_definition":
        assert c.max_holders is not None
        if c.allow_multiple != (c.max_holders > 1):
            raise ValueError("岗位人数上限与是否允许多人不一致。")
    if kind == "class_teacher_assignment" and (
        (c.teacher_role == "acting") != (c.acting_for_role is not None)
    ):
        raise ValueError("代任必须明确被代岗位，普通任职不能携带代任岗位。")
