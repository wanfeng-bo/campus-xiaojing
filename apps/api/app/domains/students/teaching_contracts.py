"""Finite F5 teaching vocabulary, independently owned from F4 authorization."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, cast

if TYPE_CHECKING:
    from app.platform.school_facts.contracts import CanonicalFactContentV3

    from .education_validation import EducationRecord

from pydantic import Field, StringConstraints

from app.platform.contracts import FrozenContractModel

SourceKey = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z0-9][a-z0-9._:-]{2,254}$")]


class TeachingContentFields(FrozenContractModel):
    subject_source_key: SourceKey | None = None
    course_source_key: SourceKey | None = None
    offering_source_key: SourceKey | None = None
    group_source_key: SourceKey | None = None
    term_source_key: SourceKey | None = None
    grade_group_source_key: SourceKey | None = None
    stage_source_keys: tuple[SourceKey, ...] | None = Field(
        default=None, min_length=1, max_length=20
    )
    subject_source_keys: tuple[SourceKey, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )
    source_kind: Literal["administrative_class", "independent_group"] | None = None
    offering_status: Literal["active", "suspended"] | None = None
    teaching_role: Literal["lead", "co_teacher"] | None = None
    teaching_role_limits: (
        dict[Literal["lead", "co_teacher"], Annotated[int, Field(strict=True, ge=1, le=100)]] | None
    ) = None
    group_mode: Literal["exclusive", "shared"] | None = None
    scope_basis: Literal["stage", "fixed_grade", "academic_grade"] | None = None


def _fields(required: str, optional: str = "") -> tuple[frozenset[str], frozenset[str]]:
    mandatory = frozenset(required.split())
    return mandatory | frozenset(optional.split()) | {"valid_to", "basis_reference"}, mandatory


TEACHING_FIELDS = {
    "teaching_subject": _fields("display_name stage_source_keys", "business_code"),
    "teaching_course": _fields("display_name", "business_code stage_source_keys grade_source_keys"),
    "course_subject": _fields("course_source_key subject_source_key"),
    "teaching_offering": _fields(
        "course_source_key offering_status",
        "display_name campus_source_key term_source_key teaching_role_limits",
    ),
    "offering_member_source": _fields(
        "offering_source_key source_kind", "class_source_key group_source_key"
    ),
    "teaching_group": _fields("organization_source_key group_mode"),
    "teaching_group_member": _fields(
        "group_source_key student_source_key school_relationship_source_key"
    ),
    "teaching_assignment": _fields(
        "offering_source_key assignment_source_key teaching_role", "subject_source_keys duty_scope"
    ),
    "teaching_organization_scope": _fields(
        "organization_source_key subject_source_key scope_basis",
        "stage_source_key grade_source_key grade_group_source_key term_source_key",
    ),
}
TEACHING_KINDS = frozenset(TEACHING_FIELDS)
TEACHING_CONTENT_FIELDS = {kind: allowed for kind, (allowed, _) in TEACHING_FIELDS.items()}
TEACHING_REFERENCES = {
    "subject_source_key": "teaching_subject",
    "course_source_key": "teaching_course",
    "offering_source_key": "teaching_offering",
    "group_source_key": "teaching_group",
    "term_source_key": "education_term",
    "grade_group_source_key": "education_grade_group",
}
TEACHING_ARRAY_REFERENCES = {
    "stage_source_keys": "education_stage",
    "subject_source_keys": "teaching_subject",
}


def validate_teaching_content(kind: str, content: object) -> None:
    c = cast("CanonicalFactContentV3", content)
    for field in (*TEACHING_ARRAY_REFERENCES, "grade_source_keys"):
        keys = getattr(content, field, None)
        if keys is not None and len(set(keys)) != len(keys):
            raise ValueError("教学引用列表不能重复。")
    if kind == "offering_member_source":
        expected = (
            "class_source_key" if c.source_kind == "administrative_class" else "group_source_key"
        )
        if (
            getattr(content, expected) is None
            or sum(
                getattr(content, f) is not None for f in ("class_source_key", "group_source_key")
            )
            != 1
        ):
            raise ValueError("开课名单必须且只能采用明确的一种正式来源。")
    if kind == "teaching_organization_scope":
        field = {
            "stage": "stage_source_key",
            "fixed_grade": "grade_source_key",
            "academic_grade": "grade_group_source_key",
        }[str(c.scope_basis)]
        if (
            getattr(content, field) is None
            or sum(
                getattr(content, f) is not None
                for f in ("stage_source_key", "grade_source_key", "grade_group_source_key")
            )
            != 1
        ):
            raise ValueError("教学组织适用口径必须具有唯一对应的正式引用。")


def validate_teaching_rules(rows: tuple[EducationRecord, ...]) -> None:
    """Check each business-time boundary over the final atomic batch state."""
    relevant = tuple(r for r in rows if r.kind in TEACHING_KINDS)
    points = {r.start for r in relevant} | {r.end for r in relevant if r.end is not None}
    identities = {
        "course_subject": ("course_source_key", "subject_source_key"),
        "offering_member_source": ("offering_source_key",),
        "teaching_group": ("organization_source_key",),
        "teaching_group_member": ("group_source_key", "student_source_key"),
        "teaching_assignment": ("offering_source_key", "assignment_source_key", "teaching_role"),
        "teaching_organization_scope": (
            "organization_source_key",
            "subject_source_key",
            "scope_basis",
            "stage_source_key",
            "grade_source_key",
            "grade_group_source_key",
            "term_source_key",
        ),
    }
    for at in points:
        active = [r for r in relevant if r.active(at)]
        seen: set[tuple[object, ...]] = set()
        for row in active:
            if row.kind in identities:
                identity = (row.kind, *(row.content.get(f) for f in identities[row.kind]))
                if identity in seen:
                    raise ValueError("同期教学关系重复，不能用不同来源编号绕过。")
                seen.add(identity)
        for offering in (r for r in active if r.kind == "teaching_offering"):
            sources = [
                r
                for r in active
                if r.kind == "offering_member_source"
                and r.content.get("offering_source_key") == offering.key
            ]
            if len(sources) != 1:
                raise ValueError("开课完整期间必须且只能有一种正式名单来源。")
            teachers = [
                r
                for r in active
                if r.kind == "teaching_assignment"
                and r.content.get("offering_source_key") == offering.key
            ]
            limits = {
                "lead": 1,
                "co_teacher": 100,
                **cast("dict[str, int]", offering.content.get("teaching_role_limits") or {}),
            }
            for role, limit in limits.items():
                if sum(r.content.get("teaching_role") == role for r in teachers) > limit:
                    raise ValueError("持续任教人数超过开课正式岗位上限。")
            subjects = {
                r.content.get("subject_source_key")
                for r in active
                if r.kind == "course_subject"
                and r.content.get("course_source_key") == offering.content.get("course_source_key")
            }
            for teacher in teachers:
                restricted = teacher.content.get("subject_source_keys")
                if (
                    restricted is not None
                    and not set(cast("tuple[str, ...]", restricted)) <= subjects
                ):
                    raise ValueError("教师学科分工超出本开课课程正式学科。")
        for group in (r for r in active if r.kind == "teaching_group"):
            uses = [
                r
                for r in active
                if r.kind == "offering_member_source"
                and r.content.get("group_source_key") == group.key
            ]
            if group.content.get("group_mode") == "exclusive" and len(uses) > 1:
                raise ValueError("专属教学群体不能被多个开课共享。")
