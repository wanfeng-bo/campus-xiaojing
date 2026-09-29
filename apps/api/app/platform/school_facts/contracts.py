from __future__ import annotations

import json
import re
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

from app.domains.students.education_contracts import (
    EDUCATION_FIELDS,
    EDUCATION_KINDS,
    EducationContentFields,
    validate_education_content,
)
from app.platform.contracts import FrozenContractModel
from app.platform.school_facts.f3_policy import F3_FACT_FIELDS

type FactOperation = Literal["upsert", "end", "revoke", "reactivate", "correct", "cancel"]
type SchoolFactKind = Literal[
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
    "dormitory_building_profile",
    "dorm_bed",
    "student_bed_residence",
    "person_contact",
    "student_contact",
    "guardian_relationship",
    "person_region",
    "official_contact",
    "policy",
    "policy_scope",
    "policy_version",
    "organization_profile",
    "organization_membership",
    "organization_location",
    "person_workplace",
    "school_service",
    "fact_quality",
    "education_stage",
    "education_offering",
    "education_grade",
    "education_calendar_rule",
    "education_term",
    "education_grade_group",
    "education_cohort",
    "student_cohort_membership",
    "education_class",
    "class_placement",
    "class_lineage",
    "student_grade_placement",
    "student_class_membership",
    "class_teacher_assignment",
    "class_location",
    "class_position_definition",
    "student_class_position",
    "teaching_subject",
    "teaching_course",
    "course_subject",
    "teaching_offering",
    "offering_member_source",
    "teaching_group",
    "teaching_group_member",
    "teaching_assignment",
    "teaching_organization_scope",
]

MAX_SCHOOL_FACT_CHANGES = 50_000
# 每项最多一个目标版本问题、四个引用问题；另有一个学校基线版本问题。
MAX_SCHOOL_FACT_PREFLIGHT_ISSUES = MAX_SCHOOL_FACT_CHANGES * 5 + 1

Sha256Hex = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
StableSourceKey = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=3,
        max_length=255,
        pattern=r"^[a-z0-9][a-z0-9._:-]*$",
    ),
]
SafeCode = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
_NEUTRAL_LINEAGE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def require_neutral_lineage_code(value: object) -> str:
    """只接受登记在服务端的中性 ASCII 血缘编码，不允许原文件/原表名形态。"""

    if not isinstance(value, str) or _NEUTRAL_LINEAGE_CODE.fullmatch(value) is None:
        raise ValueError("学校事实血缘编码无效。")
    return value


NonBlankText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1000),
]


class FactOriginV1(FrozenContractModel):
    """只保存稳定、可公开的来源坐标，不保存文件路径或原始单元格值。"""

    sheet_code: SafeCode
    row_number: int = Field(strict=True, ge=1, le=1_000_000)
    column_code: SafeCode | None = None


class CanonicalFactContentV1(FrozenContractModel):
    """17 类基础事实共用的严格语义字段全集；具体类别再做精确白名单。"""

    business_code: NonBlankText | None = None
    display_name: NonBlankText | None = None
    short_name: NonBlankText | None = None
    timezone_name: NonBlankText | None = None
    lifecycle_status: SafeCode | None = None
    person_kind: SafeCode | None = None
    person_source_key: StableSourceKey | None = None
    provider_code: SafeCode | None = None
    provider_tenant_key: NonBlankText | None = None
    external_subject: NonBlankText | None = None
    parent_source_key: StableSourceKey | None = None
    organization_source_key: StableSourceKey | None = None
    campus_source_key: StableSourceKey | None = None
    primary_campus_source_key: StableSourceKey | None = None
    administrative_campus_source_key: StableSourceKey | None = None
    location_source_key: StableSourceKey | None = None
    room_source_key: StableSourceKey | None = None
    student_source_key: StableSourceKey | None = None
    bed_source_key: StableSourceKey | None = None
    contact_person_source_key: StableSourceKey | None = None
    guardian_person_source_key: StableSourceKey | None = None
    publishing_organization_source_key: StableSourceKey | None = None
    policy_source_key: StableSourceKey | None = None
    supersedes_policy_version_source_key: StableSourceKey | None = None
    kind: SafeCode | None = None
    status: SafeCode | None = None
    display_path: NonBlankText | None = None
    position_title: NonBlankText | None = None
    semantic_role_code: SafeCode | None = None
    assignment_kind: SafeCode | None = None
    appointment_source: SafeCode | None = None
    approval_reference: NonBlankText | None = None
    alias: NonBlankText | None = None
    alias_kind: SafeCode | None = None
    aliases: tuple[NonBlankText, ...] | None = Field(default=None, max_length=100)
    authorized_bed_capacity: int | None = Field(default=None, strict=True, ge=0, le=10_000)
    accommodation_zone: SafeCode | None = None
    is_available: bool | None = None
    accessibility_tags: tuple[SafeCode, ...] | None = Field(default=None, max_length=100)
    availability: SafeCode | None = None
    student_number: NonBlankText | None = None
    enrollment_status: SafeCode | None = None
    residence_mode: Literal["day", "boarding"] | None = None
    contact_kind: SafeCode | None = None
    contact_value: NonBlankText | None = None
    is_primary: bool | None = None
    visibility_scope: SafeCode | None = None
    audience_code: SafeCode | None = None
    consent_reference: NonBlankText | None = None
    relationship_kind: SafeCode | None = None
    access_purpose: SafeCode | None = None
    verification_source: SafeCode | None = None
    verification_reference: NonBlankText | None = None
    review_of_relationship_source_key: StableSourceKey | None = None
    district_code: SafeCode | None = None
    district_name: NonBlankText | None = None
    region_purpose: SafeCode | None = None
    priority: int | None = Field(default=None, strict=True, ge=1, le=100)
    is_emergency: bool | None = None
    owner_kind: SafeCode | None = None
    service_hours: NonBlankText | None = None
    category_code: SafeCode | None = None
    classification: SafeCode | None = None
    published_at: AwareDatetime | None = None
    effective_from: AwareDatetime | None = None
    effective_to: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None
    content_text: NonBlankText | None = None
    version_note: NonBlankText | None = None


F2_FACT_KINDS = frozenset(
    {
        "organization_profile",
        "organization_membership",
        "organization_location",
        "person_workplace",
        "school_service",
        "fact_quality",
    }
)
F2_QUALITY_TARGET_KINDS = frozenset(
    {
        "school",
        "person",
        "organization",
        "assignment",
        "organization_campus_relationship",
        "location",
        "location_alias",
        "person_contact",
        "official_contact",
        "policy",
        "policy_scope",
        "policy_version",
        "organization_profile",
        "organization_membership",
        "organization_location",
        "person_workplace",
        "school_service",
    }
)
F7_FAMILY_QUALITY_TARGET_KINDS = frozenset(
    {
        "student",
        "student_school_relationship",
        "login_mapping",
        "student_contact",
        "guardian_relationship",
        "person_region",
    }
)
FullPolicyText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=100_000)]


class CanonicalFactContentV2(CanonicalFactContentV1):
    """Explicit F2 JSON content; full policy text preserves whitespace verbatim."""

    content_text: FullPolicyText | None = None

    @field_validator("content_text")
    @classmethod
    def require_nonblank_original_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Policy original text must not be blank.")
        return value

    assignment_source_key: StableSourceKey | None = None
    official_contact_source_key: StableSourceKey | None = None
    organization_type: SafeCode | None = None
    function_summary: NonBlankText | None = None
    basis_reference: NonBlankText | None = None
    membership_kind: Literal["member", "participant"] | None = None
    purpose: Literal["office", "reception"] | None = None
    audience_description: NonBlankText | None = None
    service_summary: NonBlankText | None = None
    quality_kind: (
        Literal["source", "coverage", "negative", "dispute", "review", "correction"] | None
    ) = None
    target_fact_kind: SafeCode | None = None
    target_source_key: StableSourceKey | None = None
    replacement_source_key: StableSourceKey | None = None
    responsibility: NonBlankText | None = None
    evidence_reference: NonBlankText | None = None
    coverage_status: Literal["partial", "complete"] | None = None
    negative_code: (
        Literal["no_office", "no_reception", "no_official_channel", "no_service"] | None
    ) = None
    dispute_status: Literal["open", "resolved"] | None = None
    source_status: Literal["available", "unavailable"] | None = None
    last_success_at: AwareDatetime | None = None
    verified_at: AwareDatetime | None = None
    review_due_at: AwareDatetime | None = None


class CanonicalFactContentV3(CanonicalFactContentV2, EducationContentFields):
    """Explicit education content, preserving existing v1/v2 input boundaries."""

    negative_code: (
        Literal[
            "no_office",
            "no_reception",
            "no_official_channel",
            "no_service",
            "vacant_position",
            "no_class_teacher",
            "no_fixed_location",
            "no_teaching_assignment",
        ]
        | None
    ) = None


F6_FACILITY_KINDS = frozenset({"dormitory_building_profile", "dorm_room", "dorm_bed"})
F6_FACT_KINDS = F6_FACILITY_KINDS | {"student_bed_residence"}


class CoverageBasisV1(FrozenContractModel):
    """Typed coordinates of a server-checked, bounded formal mother set.

    A structurally valid basis is not itself proof: the Writer must recompute
    the digest under its school lock and Readers must verify it again at use.
    """

    schema_version: Literal[1] = 1
    school_id: UUID
    owner_kind: Literal["school", "organization", "person", "location", "education"]
    owner_source_key: StableSourceKey | None
    target_fact_kind: SafeCode
    target_source_key: StableSourceKey | None
    field_name: SafeCode | None
    purpose: Literal["office", "reception"] | None
    contact_kind: Literal["phone", "email"] | None
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None
    school_fact_version: int = Field(strict=True, ge=0)
    member_count: int = Field(strict=True, ge=0, le=50_000)
    member_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def valid_period(self) -> Self:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("Coverage mother-set period must be positive.")
        if (self.owner_kind == "school") != (self.owner_source_key is None):
            raise ValueError("Coverage mother-set owner coordinate is incomplete.")
        return self


class CoverageBasisV2(CoverageBasisV1):
    """The one bounded F4 campus/year grade-group mother set.

    Exact object grants remain the authority. These keys are server-derived
    evidence for the final guard, never a grant or an arbitrary query.
    """

    schema_version: Literal[2] = 2
    campus_source_key: StableSourceKey
    academic_year: int = Field(strict=True, ge=1900, le=9998)
    authorized_source_keys: tuple[StableSourceKey, ...] = Field(strict=False)

    @model_validator(mode="after")
    def fixed_grade_group_set(self) -> Self:
        keys = self.authorized_source_keys
        if (
            self.owner_kind != "education"
            or self.target_fact_kind != "education_grade_group"
            or self.target_source_key is not None
            or self.field_name != "grade_source_key"
            or self.purpose is not None
            or self.contact_kind is not None
            or len(keys) > 100
            or tuple(sorted(set(keys))) != keys
            or len(keys) != self.member_count
        ):
            raise ValueError("Only an exact authorized campus/year grade-group set is supported.")
        return self


class CanonicalFactContentV4(CanonicalFactContentV3):
    """F6 adds explicit unknown enrollment and residence fields without changing V1–V3."""

    usage_kind: Literal["building", "residential_area"] | None = None
    description: NonBlankText | None = None
    coverage_basis: CoverageBasisV1 | CoverageBasisV2 | None = None
    negative_code: (
        Literal[
            "no_office",
            "no_reception",
            "no_official_channel",
            "no_service",
            "vacant_position",
            "no_class_teacher",
            "no_fixed_location",
            "no_teaching_assignment",
            "no_residence",
            "no_dormitory",
        ]
        | None
    ) = None

    @model_serializer(mode="wrap")
    def keep_explicit_unknown(self, handler):
        payload = handler(self)
        if "residence_mode" in self.model_fields_set and self.residence_mode is None:
            payload["residence_mode"] = None
        return payload


_CONTENT_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "school": (
        frozenset(
            {"business_code", "display_name", "short_name", "timezone_name", "lifecycle_status"}
        ),
        frozenset({"business_code", "display_name", "timezone_name", "lifecycle_status"}),
    ),
    "person": (
        frozenset({"business_code", "display_name", "person_kind", "lifecycle_status"}),
        frozenset({"display_name", "person_kind", "lifecycle_status"}),
    ),
    "student": (
        frozenset({"person_source_key", "status"}),
        frozenset({"person_source_key", "status"}),
    ),
    "student_school_relationship": (
        frozenset(
            {
                "student_source_key",
                "student_number",
                "enrollment_status",
                "residence_mode",
                "primary_campus_source_key",
                "administrative_campus_source_key",
            }
        ),
        frozenset(
            {
                "student_source_key",
                "student_number",
                "enrollment_status",
                "residence_mode",
                "primary_campus_source_key",
                "administrative_campus_source_key",
            }
        ),
    ),
    "login_mapping": (
        frozenset(
            {
                "provider_code",
                "provider_tenant_key",
                "external_subject",
                "person_source_key",
                "status",
            }
        ),
        frozenset(
            {
                "provider_code",
                "provider_tenant_key",
                "external_subject",
                "person_source_key",
                "status",
            }
        ),
    ),
    "organization": (
        frozenset(
            {
                "parent_source_key",
                "kind",
                "business_code",
                "display_name",
                "short_name",
                "display_path",
                "status",
            }
        ),
        frozenset({"kind", "display_name", "display_path", "status"}),
    ),
    "organization_campus_relationship": (
        frozenset({"organization_source_key", "campus_source_key", "valid_to"}),
        frozenset({"organization_source_key", "campus_source_key"}),
    ),
    "assignment": (
        frozenset(
            {
                "person_source_key",
                "organization_source_key",
                "position_title",
                "semantic_role_code",
                "assignment_kind",
                "appointment_source",
                "approval_reference",
                "status",
                "valid_to",
            }
        ),
        frozenset(
            {
                "person_source_key",
                "organization_source_key",
                "position_title",
                "semantic_role_code",
                "assignment_kind",
                "appointment_source",
                "approval_reference",
                "status",
            }
        ),
    ),
    "location": (
        frozenset(
            {
                "parent_source_key",
                "kind",
                "business_code",
                "display_name",
                "display_path",
                "aliases",
                "status",
            }
        ),
        frozenset({"kind", "display_name", "display_path", "status"}),
    ),
    "location_alias": (
        frozenset({"location_source_key", "alias", "alias_kind"}),
        frozenset({"location_source_key", "alias", "alias_kind"}),
    ),
    "dorm_room": (
        frozenset(
            {
                "room_source_key",
                "authorized_bed_capacity",
                "accommodation_zone",
                "is_available",
                "accessibility_tags",
            }
        ),
        frozenset(
            {"room_source_key", "authorized_bed_capacity", "accommodation_zone", "is_available"}
        ),
    ),
    "dormitory_building_profile": (
        frozenset(
            {
                "location_source_key",
                "usage_kind",
                "display_name",
                "description",
                "is_available",
                "valid_to",
            }
        ),
        frozenset({"location_source_key", "usage_kind", "display_name", "is_available"}),
    ),
    "dorm_bed": (
        frozenset({"room_source_key", "business_code", "display_name", "availability"}),
        frozenset({"room_source_key", "business_code", "display_name", "availability"}),
    ),
    "student_bed_residence": (
        frozenset({"student_source_key", "bed_source_key", "valid_to"}),
        frozenset({"student_source_key", "bed_source_key"}),
    ),
    "person_contact": (
        frozenset(
            {
                "person_source_key",
                "contact_kind",
                "contact_value",
                "is_primary",
                "visibility_scope",
                "audience_code",
                "consent_reference",
            }
        ),
        frozenset(
            {"person_source_key", "contact_kind", "contact_value", "is_primary", "visibility_scope"}
        ),
    ),
    "student_contact": (
        frozenset(
            {
                "student_source_key",
                "contact_person_source_key",
                "relationship_kind",
                "priority",
                "is_emergency",
            }
        ),
        frozenset(
            {
                "student_source_key",
                "contact_person_source_key",
                "relationship_kind",
                "priority",
                "is_emergency",
            }
        ),
    ),
    "guardian_relationship": (
        frozenset(
            {
                "guardian_person_source_key",
                "student_source_key",
                "relationship_kind",
                "access_purpose",
                "verification_source",
                "verification_reference",
                "review_of_relationship_source_key",
                "status",
            }
        ),
        frozenset(
            {
                "guardian_person_source_key",
                "student_source_key",
                "relationship_kind",
                "access_purpose",
                "verification_source",
                "verification_reference",
                "status",
            }
        ),
    ),
    "person_region": (
        frozenset({"person_source_key", "district_code", "district_name", "region_purpose"}),
        frozenset({"person_source_key", "district_code", "district_name", "region_purpose"}),
    ),
    "official_contact": (
        frozenset(
            {
                "owner_kind",
                "organization_source_key",
                "location_source_key",
                "contact_kind",
                "contact_value",
                "service_hours",
                "lifecycle_status",
                "visibility_scope",
                "audience_code",
            }
        ),
        frozenset(
            {"owner_kind", "contact_kind", "contact_value", "lifecycle_status", "visibility_scope"}
        ),
    ),
    "policy": (frozenset(), frozenset()),
    "policy_scope": (
        frozenset(
            {
                "policy_source_key",
                "organization_source_key",
                "campus_source_key",
                "valid_to",
            }
        ),
        frozenset({"policy_source_key"}),
    ),
    "policy_version": (
        frozenset(
            {
                "policy_source_key",
                "business_code",
                "display_name",
                "category_code",
                "classification",
                "publishing_organization_source_key",
                "published_at",
                "effective_from",
                "effective_to",
                "content_text",
                "version_note",
                "supersedes_policy_version_source_key",
            }
        ),
        frozenset(
            {
                "policy_source_key",
                "business_code",
                "display_name",
                "category_code",
                "classification",
                "published_at",
                "effective_from",
            }
        ),
    ),
}

for _kind, _required, _optional in (
    (
        "organization_profile",
        {"organization_source_key", "organization_type", "function_summary", "basis_reference"},
        set(),
    ),
    (
        "organization_membership",
        {"person_source_key", "organization_source_key", "membership_kind", "basis_reference"},
        set(),
    ),
    (
        "organization_location",
        {
            "organization_source_key",
            "location_source_key",
            "purpose",
            "is_primary",
            "basis_reference",
        },
        set(),
    ),
    (
        "person_workplace",
        {"location_source_key", "purpose", "is_primary", "basis_reference"},
        {"person_source_key", "assignment_source_key"},
    ),
    (
        "school_service",
        {
            "display_name",
            "organization_source_key",
            "audience_description",
            "service_summary",
            "basis_reference",
        },
        {"official_contact_source_key", "policy_source_key"},
    ),
    (
        "fact_quality",
        {
            "quality_kind",
            "target_fact_kind",
            "owner_kind",
            "basis_reference",
            "responsibility",
            "verified_at",
        },
        {
            "organization_source_key",
            "person_source_key",
            "location_source_key",
            "target_source_key",
            "replacement_source_key",
            "purpose",
            "contact_kind",
            "evidence_reference",
            "coverage_status",
            "negative_code",
            "dispute_status",
            "source_status",
            "last_success_at",
            "review_due_at",
        },
    ),
):
    _CONTENT_FIELDS[_kind] = (frozenset(_required | _optional | {"valid_to"}), frozenset(_required))


_ENUM_FIELDS: dict[str, dict[str, frozenset[str]]] = {
    "school": {"lifecycle_status": frozenset({"active", "inactive"})},
    "person": {
        "person_kind": frozenset({"staff", "student_contact", "other"}),
        "lifecycle_status": frozenset({"active", "inactive"}),
    },
    "student": {"status": frozenset({"active", "inactive", "merged"})},
    "student_school_relationship": {
        "enrollment_status": frozenset(
            {"enrolled", "suspended", "transferred_out", "withdrawn", "graduated"}
        )
    },
    "login_mapping": {"status": frozenset({"draft", "confirmed", "rejected"})},
    "organization": {"status": frozenset({"active", "inactive"})},
    "assignment": {
        "assignment_kind": frozenset({"substantive", "part_time", "acting"}),
        "status": frozenset({"draft", "confirmed", "rejected"}),
    },
    "location": {"status": frozenset({"active", "inactive"})},
    "location_alias": {"alias_kind": frozenset({"former", "common", "short"})},
    "dorm_bed": {"availability": frozenset({"available", "unavailable", "inactive"})},
    "person_contact": {
        "contact_kind": frozenset({"mobile", "phone", "email"}),
        "visibility_scope": frozenset({"private", "school_authenticated", "declared_audience"}),
    },
    "student_contact": {
        "relationship_kind": frozenset({"father", "mother", "grandfather", "grandmother", "other"})
    },
    "guardian_relationship": {
        "relationship_kind": frozenset({"verified_guardian", "recognized_caregiver"}),
        "access_purpose": frozenset({"guardian_student_profile", "caregiver_student_summary"}),
        "status": frozenset({"pending_verification", "confirmed"}),
    },
    "person_region": {"region_purpose": frozenset({"student_profile"})},
    "official_contact": {
        "owner_kind": frozenset({"school", "organization", "location"}),
        "contact_kind": frozenset({"phone", "email"}),
        "lifecycle_status": frozenset({"active", "inactive"}),
        "visibility_scope": frozenset({"public", "school_authenticated", "restricted"}),
    },
    "policy_version": {"classification": frozenset({"public", "internal", "restricted"})},
}


class EducationReplacementSegment(FrozenContractModel):
    effective_at: AwareDatetime
    valid_to: AwareDatetime | None
    content: CanonicalFactContentV4
    cause_source_key: StableSourceKey | None = None
    cause_version: int | None = Field(default=None, strict=True, ge=1)

    @model_validator(mode="after")
    def interval_and_cause(self) -> Self:
        if self.valid_to is not None and self.valid_to <= self.effective_at:
            raise ValueError("Replacement segment must have a positive interval.")
        if "valid_to" in self.content.model_fields_set:
            raise ValueError("Segment endpoints belong to the segment, not its content.")
        if (self.cause_source_key is None) != (self.cause_version is None):
            raise ValueError("Segment cause requires its exact source version.")
        return self


class CanonicalFactChangeV1(FrozenContractModel):
    """一项与客户端身份、权限和数据库列彻底分离的规范事实变更。"""

    schema_version: Literal[1, 2, 3, 4] = 1
    operation: FactOperation
    fact_kind: SchoolFactKind
    source_key: StableSourceKey
    expected_version: int = Field(strict=True, ge=0)
    effective_at: AwareDatetime
    origin: FactOriginV1
    content: (
        CanonicalFactContentV4
        | CanonicalFactContentV3
        | CanonicalFactContentV2
        | CanonicalFactContentV1
        | None
    )
    correction_reason: NonBlankText | None = None
    cause_source_key: StableSourceKey | None = None
    cause_version: int | None = Field(default=None, strict=True, ge=1)
    replacement_segments: tuple[EducationReplacementSegment, ...] | None = Field(
        default=None, max_length=1000
    )

    @field_validator("content", mode="before")
    @classmethod
    def parse_versioned_content(
        cls, value: object, info: ValidationInfo
    ) -> CanonicalFactContentV1 | None:
        if value is None:
            return None
        content_type = (
            CanonicalFactContentV4
            if info.data.get("schema_version", 1) == 4
            else CanonicalFactContentV3
            if info.data.get("schema_version", 1) == 3
            else CanonicalFactContentV2
            if info.data.get("schema_version", 1) == 2
            else CanonicalFactContentV1
        )
        if isinstance(value, CanonicalFactContentV1):
            value = value.model_dump(exclude_unset=True)
        if info.mode == "json":
            return content_type.model_validate_json(json.dumps(value))
        return content_type.model_validate(value)

    @model_validator(mode="after")
    def require_exact_content_for_fact_and_operation(self) -> Self:
        if (
            self.schema_version == 4
            and self.fact_kind == "student_bed_residence"
            and self.operation == "upsert"
            and self.expected_version != 0
        ):
            raise ValueError(
                "A residence identity is immutable; end/revoke and publish a new fact."
            )
        if self.fact_kind == "dormitory_building_profile" and self.schema_version != 4:
            raise ValueError("Dormitory profiles require canonical schema 4.")
        if self.replacement_segments is not None:
            if (
                self.schema_version not in {3, 4}
                or self.operation != "correct"
                or self.expected_version == 0
                or not self.correction_reason
                or self.content is not None
                or self.fact_kind
                not in EDUCATION_KINDS
                | {"student_school_relationship"}
                | (F6_FACILITY_KINDS if self.schema_version == 4 else frozenset())
            ):
                raise ValueError("A complete timeline requires an explicit versioned correction.")
            if (self.cause_source_key is None) != (self.cause_version is None):
                raise ValueError("Correction cause requires its exact source version.")
            previous = None
            for segment in self.replacement_segments:
                if previous is not None and (
                    previous.valid_to is None or segment.effective_at < previous.valid_to
                ):
                    raise ValueError("Replacement timeline must be ordered and nonoverlapping.")
                segment_content = (
                    segment.content.model_copy(update={"valid_to": segment.valid_to})
                    if segment.valid_to is not None
                    else segment.content
                )
                nested = self.model_copy(
                    update={
                        "replacement_segments": None,
                        "content": segment_content,
                        "effective_at": segment.effective_at,
                    }
                )
                nested.require_exact_content_for_fact_and_operation()
                previous = segment
            return self
        if self.schema_version == 4 and self.fact_kind in F6_FACILITY_KINDS:
            if self.operation not in {"upsert", "correct", "end", "revoke"}:
                raise ValueError("Unsupported residence facility operation.")
            if self.operation == "correct" and (
                not self.correction_reason or self.expected_version == 0
            ):
                raise ValueError("Facility correction requires an existing revision and reason.")
            if self.cause_source_key is not None or self.cause_version is not None:
                raise ValueError("Facility corrections do not imply a causal restoration.")
            if self.operation in {"upsert", "correct"}:
                allowed, required = _CONTENT_FIELDS[self.fact_kind]
                c = self.content
                if (
                    c is None
                    or not c.model_fields_set <= allowed | {"valid_to"}
                    or not required <= c.model_fields_set
                ):
                    raise ValueError("Residence facility content does not match its exact fields.")
                if any(getattr(c, field) is None for field in required):
                    raise ValueError("Residence facility required fields cannot be null.")
                if c.valid_to is not None and c.valid_to <= self.effective_at:
                    raise ValueError("Residence facility interval must be positive.")
                if self.fact_kind == "dorm_bed" and c.availability not in {
                    "available",
                    "unavailable",
                    "inactive",
                }:
                    raise ValueError("Bed availability is not registered.")
            elif self.content is not None:
                raise ValueError("Facility terminal operations cannot carry replacement content.")
            return self
        if self.fact_kind in EDUCATION_KINDS:
            if self.schema_version not in {3, 4} or self.operation == "reactivate":
                raise ValueError("Education facts require schema 3 and an education operation.")
            if self.operation in {"correct", "cancel"} and (
                not self.correction_reason or self.expected_version == 0
            ):
                raise ValueError(
                    "Correction/cancellation requires the existing version and reason."
                )
            if (self.cause_source_key is None) != (self.cause_version is None):
                raise ValueError("Causal reference and version must be supplied together.")
            if self.operation in {"upsert", "correct"}:
                if self.content is None:
                    raise ValueError("Education replacement content is required.")
                validate_education_content(self.fact_kind, self.content)
                if self.content.valid_to is not None and self.content.valid_to <= self.effective_at:
                    raise ValueError("Education interval must be positive.")
            elif self.content is not None:
                raise ValueError("Education terminal operations cannot carry content.")
            return self
        if (
            self.schema_version in {3, 4}
            and self.content is not None
            and self.content.education_target_source_key is not None
        ):
            c = self.content
            if self.operation != "upsert" or self.fact_kind not in {"policy_scope", "fact_quality"}:
                raise ValueError("Education targets require a typed scope or quality upsert.")
            if c.education_target_kind not in EDUCATION_KINDS:
                raise ValueError("Education target kind is not registered.")
            if c.valid_to is not None and c.valid_to <= self.effective_at:
                raise ValueError("Scope/quality interval must be positive.")
            extra = {
                "education_target_source_key",
                "education_target_kind",
                "academic_year",
                "field_name",
                "position_source_key",
                "teaching_role",
            }
            if self.fact_kind == "policy_scope":
                extra -= {"field_name", "position_source_key", "teaching_role"}
            elif self.schema_version == 4:
                extra.add("coverage_basis")
                if isinstance(c.coverage_basis, CoverageBasisV2):
                    extra.add("campus_source_key")
            allowed, required = _CONTENT_FIELDS[self.fact_kind]
            if not c.model_fields_set <= allowed | extra or not required <= c.model_fields_set:
                raise ValueError("Education scope/quality has invalid fields.")
            if any(getattr(c, field) is None for field in required):
                raise ValueError("Education scope/quality required fields cannot be null.")
            if self.fact_kind == "policy_scope":
                if c.organization_source_key is not None or c.policy_source_key is None:
                    raise ValueError("An education scope cannot also target an organization.")
            else:
                if (
                    c.owner_kind != "education"
                    or c.target_fact_kind not in EDUCATION_KINDS
                    or not c.responsibility
                    or not c.basis_reference
                ):
                    raise ValueError(
                        "Education quality must name its exact education owner and family."
                    )
                if any(
                    x is not None
                    for x in (c.organization_source_key, c.person_source_key, c.location_source_key)
                ):
                    raise ValueError("Education quality cannot borrow a different owner.")
                required_quality = {
                    "source": {"source_status"},
                    "coverage": {"coverage_status"},
                    "negative": {"negative_code"},
                    "dispute": {"dispute_status", "evidence_reference"},
                    "review": {"review_due_at"},
                    "correction": {
                        "target_source_key",
                        "replacement_source_key",
                        "evidence_reference",
                    },
                }[c.quality_kind]
                if any(getattr(c, field) is None for field in required_quality):
                    raise ValueError("Quality evidence is incomplete.")
                specialized = {
                    "coverage_status",
                    "negative_code",
                    "dispute_status",
                    "source_status",
                    "replacement_source_key",
                }
                if any(getattr(c, field) is not None for field in specialized - required_quality):
                    raise ValueError("Education quality cannot mix evidence kinds.")
                if (
                    c.field_name is not None
                    and c.field_name not in EDUCATION_FIELDS[c.target_fact_kind][0]
                ):
                    raise ValueError("Education quality field is not owned by the target family.")
                if (
                    c.replacement_source_key is not None
                    and c.replacement_source_key == c.target_source_key
                ):
                    raise ValueError("Correction requires distinct original and replacement facts.")
                if c.review_due_at is not None and c.review_due_at <= c.verified_at:
                    raise ValueError("Review deadline must follow verification.")
                if c.teaching_role is not None and c.target_fact_kind != "teaching_assignment":
                    raise ValueError("Teaching role quality must target teaching assignments.")
                if c.negative_code == "no_teaching_assignment" and (
                    c.education_target_kind != "teaching_offering"
                    or c.teaching_role is None
                    or c.position_source_key is not None
                    or c.target_source_key is not None
                    or c.replacement_source_key is not None
                ):
                    raise ValueError("Teaching vacancy requires its offering and exact role.")
                if c.quality_kind == "negative":
                    pairs = {
                        "vacant_position": "student_class_position",
                        "no_class_teacher": "class_teacher_assignment",
                        "no_fixed_location": "class_location",
                        "no_teaching_assignment": "teaching_assignment",
                    }
                    if pairs.get(c.negative_code) != c.target_fact_kind or (
                        c.negative_code == "vacant_position" and c.position_source_key is None
                    ):
                        raise ValueError(
                            "Formal negative must identify the exact relation/position."
                        )
                if self.schema_version == 4:
                    basis = c.coverage_basis
                    complete = c.quality_kind == "coverage" and c.coverage_status == "complete"
                    if complete != (basis is not None):
                        raise ValueError("Complete education coverage requires its typed basis.")
                    if basis is not None and (
                        basis.owner_kind != "education"
                        or basis.owner_source_key != c.education_target_source_key
                        or basis.target_fact_kind != c.target_fact_kind
                        or basis.target_source_key != c.target_source_key
                        or basis.field_name != c.field_name
                        or basis.purpose != c.purpose
                        or basis.contact_kind != c.contact_kind
                        or basis.valid_from != self.effective_at
                        or basis.valid_to != c.valid_to
                    ):
                        raise ValueError("Education coverage basis does not match the claim.")
                    if isinstance(basis, CoverageBasisV2) and (
                        c.education_target_kind != "education_calendar_rule"
                        or c.campus_source_key != basis.campus_source_key
                        or c.academic_year != basis.academic_year
                    ):
                        raise ValueError("Campus/year coverage coordinates do not match.")
            return self
        if (
            self.schema_version in {3, 4}
            and self.fact_kind == "student_school_relationship"
            and self.operation == "correct"
        ):
            if self.expected_version == 0 or not self.correction_reason or self.content is None:
                raise ValueError(
                    "School enrollment correction requires current version, reason and "
                    "exact content."
                )
            allowed, required = _CONTENT_FIELDS[self.fact_kind]
            if (
                not self.content.model_fields_set <= allowed | {"valid_to"}
                or not required <= self.content.model_fields_set
            ):
                raise ValueError("School enrollment correction has invalid fields.")
            if any(
                getattr(self.content, field) is None
                for field in required
                if not (self.schema_version == 4 and field == "residence_mode")
            ):
                raise ValueError("Enrollment correction required fields cannot be null.")
            if (
                self.content.enrollment_status
                not in _ENUM_FIELDS[self.fact_kind]["enrollment_status"]
            ):
                raise ValueError("Enrollment status is not registered.")
            if self.content.valid_to is not None and self.content.valid_to <= self.effective_at:
                raise ValueError("Enrollment correction interval must be positive.")
            return self
        if (
            self.operation in {"correct", "cancel"}
            or self.cause_source_key is not None
            or self.correction_reason is not None
        ):
            raise ValueError("Education correction metadata cannot modify other fact families.")
        if self.schema_version == 1:
            if self.fact_kind in F2_FACT_KINDS:
                raise ValueError("F2 facts require explicit canonical schema version 2.")
            if self.content is not None:
                CanonicalFactContentV1.model_validate(self.content.model_dump(exclude_unset=True))
        content_operations = {"upsert", "reactivate"}
        if self.operation not in content_operations:
            if self.content is not None:
                raise ValueError("结束或撤销操作不能携带替换内容。")
            return self
        if self.content is None:
            raise ValueError("upsert 或 reactivate 必须携带规范内容。")
        if self.operation == "reactivate":
            if self.fact_kind not in {"school", "person", "student", "organization", "location"}:
                raise ValueError("reactivate 只允许重新启用根身份事实。")
            if self.expected_version == 0:
                raise ValueError("reactivate 必须提供已有根身份的当前版本。")
        if self.fact_kind in F2_FACT_KINDS:
            self._validate_f2_content()
        allowed, required = _CONTENT_FIELDS[self.fact_kind]
        if self.schema_version in {3, 4} and self.fact_kind == "student_school_relationship":
            allowed = allowed | {"valid_to"}
        if self.schema_version == 4 and self.fact_kind == "fact_quality":
            allowed = allowed | {"field_name", "coverage_basis"}
        provided = frozenset(self.content.model_fields_set)
        if not provided.issubset(allowed):
            raise ValueError("事实内容包含不属于该事实类别的字段。")
        if not required.issubset(provided):
            raise ValueError("事实内容缺少该事实类别的必填字段。")
        if any(
            getattr(self.content, name) is None
            for name in required
            if not (
                self.schema_version == 4
                and self.fact_kind == "student_school_relationship"
                and name == "residence_mode"
            )
        ):
            raise ValueError("事实内容必填字段不能为空。")
        for field_name, allowed_values in _ENUM_FIELDS.get(self.fact_kind, {}).items():
            value = getattr(self.content, field_name)
            if value is not None and value not in allowed_values:
                raise ValueError("规范事实包含未登记的枚举值。")
        if self.operation in {"upsert", "reactivate"} and self.fact_kind in {
            "school",
            "person",
            "student",
            "organization",
            "location",
        }:
            active_field = (
                "lifecycle_status" if self.fact_kind in {"school", "person"} else "status"
            )
            if getattr(self.content, active_field) != "active":
                raise ValueError("根身份内容写入终态必须为 active。")
        if self.fact_kind == "policy_scope":
            targets = (
                self.content.organization_source_key,
                self.content.campus_source_key,
            )
            if sum(target is not None for target in targets) != 1:
                raise ValueError("制度适用范围必须且只能指向一个校区或组织。")
        if self.fact_kind == "guardian_relationship":
            expected_purpose = {
                "verified_guardian": "guardian_student_profile",
                "recognized_caregiver": "caregiver_student_summary",
            }.get(str(self.content.relationship_kind))
            if self.content.access_purpose != expected_purpose:
                raise ValueError("家校关系类型与获准用途不匹配。")
        if self.fact_kind == "person_contact":
            visibility = self.content.visibility_scope
            audience = self.content.audience_code
            if (visibility == "declared_audience") != (audience is not None):
                raise ValueError("个人联系方式可见范围与受众不匹配。")
        if self.fact_kind == "official_contact":
            owner_refs = {
                "school": (None, None),
                "organization": (self.content.organization_source_key, None),
                "location": (None, self.content.location_source_key),
            }
            expected_refs = owner_refs.get(str(self.content.owner_kind))
            actual_refs = (
                self.content.organization_source_key,
                self.content.location_source_key,
            )
            if expected_refs is None or actual_refs != expected_refs:
                raise ValueError("官方联系方式拥有者引用不匹配。")
            if (self.content.visibility_scope == "restricted") != (
                self.content.audience_code is not None
            ):
                raise ValueError("官方联系方式可见范围与受众不匹配。")
        if (
            self.fact_kind == "policy_version"
            and self.content.effective_to is not None
            and self.content.effective_from is not None
            and self.content.effective_to <= self.content.effective_from
        ):
            raise ValueError("制度版本结束时间必须晚于开始时间。")
        if (
            self.fact_kind
            in {
                "assignment",
                "organization_campus_relationship",
                "policy_scope",
                "student_bed_residence",
            }
            and self.content.valid_to is not None
            and self.content.valid_to <= self.effective_at
        ):
            raise ValueError("事实结束时间必须晚于开始时间。")
        return self

    def _validate_f2_content(self) -> None:
        c = self.content
        if not isinstance(c, CanonicalFactContentV2):
            raise ValueError("F2 content required.")
        if c.valid_to is not None and c.valid_to <= self.effective_at:
            raise ValueError("F2 valid interval must be positive.")
        if self.fact_kind == "person_workplace" and (
            (c.person_source_key is None) == (c.assignment_source_key is None)
        ):
            raise ValueError("Workplace requires exactly one person or assignment.")
        if self.fact_kind == "organization_profile" and c.organization_type not in {
            "department",
            "grade_group",
            "teaching_research_group",
            "lesson_preparation_group",
            "committee",
            "other",
        }:
            raise ValueError("Organization profile type is not registered.")
        if self.fact_kind != "fact_quality":
            return
        if c.contact_kind not in {None, "phone", "email"}:
            raise ValueError("Quality contact kind must name a registered official channel.")
        if c.target_fact_kind not in F2_QUALITY_TARGET_KINDS | (
            F6_FACT_KINDS | F7_FAMILY_QUALITY_TARGET_KINDS
            if self.schema_version == 4
            else frozenset()
        ):
            raise ValueError("Quality target is outside F2.")
        refs = (
            c.organization_source_key is not None,
            c.person_source_key is not None,
            c.location_source_key is not None,
        )
        if refs != {
            "school": (False, False, False),
            "organization": (True, False, False),
            "person": (False, True, False),
            "location": (False, False, True),
        }.get(c.owner_kind or ""):
            raise ValueError("Quality owner references do not match scope.")
        if c.target_fact_kind in F7_FAMILY_QUALITY_TARGET_KINDS:
            if (
                c.owner_kind != "person"
                or c.person_source_key is None
                or c.target_source_key is None
                or getattr(c, "field_name", None) is not None
                and c.field_name not in F3_FACT_FIELDS[c.target_fact_kind]
            ):
                raise ValueError("Family quality requires an exact person, target and owned field.")
        if self.schema_version == 4:
            basis = c.coverage_basis
            complete = c.quality_kind == "coverage" and c.coverage_status == "complete"
            if complete != (basis is not None):
                raise ValueError("Complete coverage requires its exact typed mother-set basis.")
            if basis is not None:
                expected_owner = {
                    "school": None,
                    "organization": c.organization_source_key,
                    "person": c.person_source_key,
                    "location": c.location_source_key,
                    "education": c.education_target_source_key,
                }.get(c.owner_kind)
                if (
                    basis.owner_kind != c.owner_kind
                    or basis.owner_source_key != expected_owner
                    or basis.target_fact_kind != c.target_fact_kind
                    or basis.target_source_key != c.target_source_key
                    or basis.field_name != c.field_name
                    or basis.purpose != c.purpose
                    or basis.contact_kind != c.contact_kind
                    or basis.valid_from != self.effective_at
                    or basis.valid_to != c.valid_to
                    or c.target_source_key is not None and basis.member_count != 1
                ):
                    raise ValueError("Complete coverage basis does not match the claim coordinate.")
                if isinstance(basis, CoverageBasisV2) and (
                    c.education_target_kind != "education_calendar_rule"
                    or c.campus_source_key != basis.campus_source_key
                    or c.academic_year != basis.academic_year
                    or c.field_name != "grade_source_key"
                ):
                    raise ValueError("Campus/year coverage must match its exact education claim.")
        elif getattr(c, "field_name", None) is not None:
            raise ValueError("Only a typed F3 target may use this field coordinate.")
        required = {
            "source": {"source_status"},
            "coverage": {"coverage_status"},
            "negative": {"negative_code"},
            "dispute": {"dispute_status", "evidence_reference"},
            "review": {"review_due_at"},
            "correction": {"target_source_key", "replacement_source_key", "evidence_reference"},
        }.get(c.quality_kind or "", set())
        if any(getattr(c, name) is None for name in required):
            raise ValueError("Quality kind requires its typed evidence fields.")
        specialized = {
            "coverage_status",
            "negative_code",
            "dispute_status",
            "source_status",
            "replacement_source_key",
        }
        if any(getattr(c, name) is not None for name in specialized - required):
            raise ValueError("Quality record mixes incompatible quality kinds.")
        if c.quality_kind == "negative":
            allowed = {
                "no_office": ({"organization_location", "person_workplace"}, "office"),
                "no_reception": ({"organization_location", "person_workplace"}, "reception"),
                "no_official_channel": ({"official_contact"}, None),
                "no_service": ({"school_service"}, None),
                "no_residence": ({"student_bed_residence"}, None),
                "no_dormitory": ({"dormitory_building_profile"}, None),
            }
            kinds, purpose = allowed.get(c.negative_code or "", (set(), None))
            if c.target_fact_kind not in kinds or c.purpose != purpose:
                raise ValueError("Formal negative does not match its relationship or purpose.")
            if c.negative_code == "no_residence" and c.owner_kind != "person":
                raise ValueError("No-residence evidence must identify the student's person.")
            if c.negative_code == "no_dormitory" and c.owner_kind not in {"school", "location"}:
                raise ValueError("No-dormitory evidence must identify a school or location.")
            if c.negative_code == "no_official_channel" and c.contact_kind not in {
                "phone",
                "email",
            }:
                raise ValueError("Official negative requires the exact channel.")
        if (
            c.review_due_at is not None
            and c.verified_at is not None
            and c.review_due_at <= c.verified_at
        ):
            raise ValueError("Next review must follow verification.")
        if c.replacement_source_key is not None and c.replacement_source_key == c.target_source_key:
            raise ValueError("Correction requires distinct old and replacement facts.")


class CanonicalFactChangeSetV1(FrozenContractModel):
    """一次预检和确认绑定的完整规范变更集合。"""

    schema_version: Literal[1, 2, 3, 4] = 1
    source_sha256: Sha256Hex
    base_version: int = Field(strict=True, ge=1)
    changes: tuple[CanonicalFactChangeV1, ...] = Field(
        min_length=1,
        max_length=MAX_SCHOOL_FACT_CHANGES,
    )

    @model_validator(mode="after")
    def reject_duplicate_fact_targets(self) -> Self:
        if any(item.schema_version != self.schema_version for item in self.changes):
            raise ValueError("Change and plan schema versions must match.")
        targets = tuple((item.fact_kind, item.source_key) for item in self.changes)
        if len(set(targets)) != len(targets):
            raise ValueError("同一变更集不能重复变更同一稳定事实。")
        if len(self.changes) != 1 and any(item.operation == "reactivate" for item in self.changes):
            raise ValueError("重新启用必须作为独立变更集确认。")
        return self


class CanonicalFactChangeV2(CanonicalFactChangeV1):
    schema_version: Literal[1, 2, 3] = 2


class CanonicalFactChangeSetV2(CanonicalFactChangeSetV1):
    schema_version: Literal[1, 2, 3] = 2


class CanonicalFactChangeV3(CanonicalFactChangeV1):
    schema_version: Literal[1, 2, 3] = 3


class CanonicalFactChangeSetV3(CanonicalFactChangeSetV1):
    schema_version: Literal[1, 2, 3] = 3


class CanonicalFactChangeV4(CanonicalFactChangeV1):
    schema_version: Literal[4] = 4


class CanonicalFactChangeSetV4(CanonicalFactChangeSetV1):
    schema_version: Literal[4] = 4


class PreflightIssueV1(FrozenContractModel):
    sheet_code: SafeCode
    row_number: int = Field(strict=True, ge=1)
    column_code: SafeCode
    error_code: SafeCode


class PreflightReportV1(FrozenContractModel):
    plan_sha256: Sha256Hex
    is_clean: bool
    issues: tuple[PreflightIssueV1, ...] = Field(max_length=MAX_SCHOOL_FACT_PREFLIGHT_ISSUES)
    confirmation_credential: None = None


class ConfirmationReceiptV1(FrozenContractModel):
    plan_sha256: Sha256Hex
    credential: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{40,128}$"),
    ]
    expires_at: AwareDatetime


def canonical_plan_sha256(change_set: CanonicalFactChangeSetV1) -> str:
    """以固定 JSON 规则计算跨入口一致的规范计划摘要。"""

    payload = change_set.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def safe_content_payload(content: CanonicalFactContentV1 | None) -> dict[str, object] | None:
    """输出只含已显式提供字段的规范 JSON 内容。"""

    if content is None:
        return None
    return content.model_dump(mode="json", exclude_none=True, exclude_unset=True)


def ensure_aware(value: datetime) -> datetime:
    """为组合层提供显式时区防御；Pydantic 路径已保证该条件。"""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须携带时区。")
    return value


def fact_content_fields(
    fact_kind: str, schema_version: int = 2
) -> tuple[frozenset[str], frozenset[str]]:
    """Public immutable canonical field catalog; all conditional rules stay authoritative here."""
    if fact_kind in EDUCATION_KINDS:
        if schema_version not in {3, 4}:
            raise ValueError("Education facts require schema 3.")
        return EDUCATION_FIELDS[fact_kind]
    if schema_version not in {1, 2, 3, 4} or (schema_version == 1 and fact_kind in F2_FACT_KINDS):
        raise ValueError("Unsupported fact contract version.")
    if fact_kind not in _CONTENT_FIELDS:
        raise ValueError("Unregistered fact kind.")
    if fact_kind == "dormitory_building_profile" and schema_version != 4:
        raise ValueError("Dormitory profiles require schema 4.")
    if fact_kind == "fact_quality" and schema_version == 4:
        allowed, required = _CONTENT_FIELDS[fact_kind]
        return (
            allowed
            | frozenset(
                {
                    "education_target_source_key",
                    "education_target_kind",
                    "academic_year",
                    "field_name",
                    "position_source_key",
                    "teaching_role",
                    "coverage_basis",
                }
            ),
            required,
        )
    if schema_version == 4 and fact_kind in F6_FACILITY_KINDS | {"student_school_relationship"}:
        allowed, required = _CONTENT_FIELDS[fact_kind]
        return allowed | {"valid_to"}, required
    if schema_version in {3, 4} and fact_kind in {"policy_scope", "fact_quality"}:
        allowed, required = _CONTENT_FIELDS[fact_kind]
        extra = {"education_target_source_key", "education_target_kind", "academic_year"}
        if fact_kind == "fact_quality":
            extra |= {"field_name", "position_source_key", "teaching_role"}
            if schema_version == 4:
                extra.add("campus_source_key")
        return allowed | frozenset(extra), required
    return _CONTENT_FIELDS[fact_kind]


def fact_content_enums(fact_kind: str) -> dict[str, frozenset[str]]:
    """Return an independent mapping of the finite values allowed by a fact kind."""
    if fact_kind in EDUCATION_KINDS:
        values = {
            "source_kind": {"administrative_class", "independent_group"},
            "offering_status": {"active", "suspended"},
            "teaching_role": {"lead", "co_teacher"},
            "group_mode": {"exclusive", "shared"},
            "scope_basis": {"stage", "fixed_grade", "academic_grade"},
            "cohort_kind": {"original_intake", "school_transfer_intake", "expected_graduation"},
            "membership_status": {"assigned", "unassigned"},
            "teacher_role": {"primary", "deputy", "joint", "acting"},
            "teacher_mode": {"single_primary", "joint"},
            "acting_for_role": {"primary", "deputy", "joint"},
            "location_purpose": {"main_classroom", "other_fixed"},
            "lineage_kind": {"regrouped_from", "split_from", "merged_from"},
        }
        return {
            field: frozenset(choices)
            for field, choices in values.items()
            if field in EDUCATION_FIELDS[fact_kind][0]
        }
    allowed, _required = fact_content_fields(fact_kind)
    result = dict(_ENUM_FIELDS.get(fact_kind, {}))
    f2_values = {
        "organization_type": frozenset(
            {
                "department",
                "grade_group",
                "teaching_research_group",
                "lesson_preparation_group",
                "committee",
                "other",
            }
        ),
        "membership_kind": frozenset({"member", "participant"}),
        "purpose": frozenset({"office", "reception"}),
        "quality_kind": frozenset(
            {"source", "coverage", "negative", "dispute", "review", "correction"}
        ),
        "target_fact_kind": F2_QUALITY_TARGET_KINDS,
        "coverage_status": frozenset({"partial", "complete"}),
        "negative_code": frozenset(
            {"no_office", "no_reception", "no_official_channel", "no_service"}
        ),
        "dispute_status": frozenset({"open", "resolved"}),
        "source_status": frozenset({"available", "unavailable"}),
    }
    if fact_kind == "fact_quality":
        result.update(
            owner_kind=frozenset({"school", "organization", "person", "location"}),
            contact_kind=frozenset({"phone", "email"}),
        )
    result.update({field: values for field, values in f2_values.items() if field in allowed})
    return result
