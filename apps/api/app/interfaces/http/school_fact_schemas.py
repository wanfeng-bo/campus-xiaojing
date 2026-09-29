from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)

from app.platform.master_data_imports.school_facts_package import FullScopeInputManifestV5
from app.platform.master_data_imports.status_mappings import (
    MAX_SCHOOL_FACT_STATUS_DECISIONS,
    require_safe_status_original_value,
)
from app.platform.school_facts.contracts import (
    MAX_SCHOOL_FACT_CHANGES,
    MAX_SCHOOL_FACT_PREFLIGHT_ISSUES,
    CanonicalFactContentV1,
    CanonicalFactContentV4,
    FactOperation,
)

OpaqueRef = Annotated[
    str,
    StringConstraints(strict=True, min_length=10, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
]
Sha256Hex = Annotated[
    str,
    StringConstraints(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
NeutralSourceCode = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"),
]


class _StrictHttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SchoolFactUploadRequest(_StrictHttpModel):
    filename_summary: Annotated[
        str,
        StringConstraints(strict=True, strip_whitespace=True, min_length=6, max_length=255),
    ]
    content_base64: Annotated[
        str,
        StringConstraints(strict=True, min_length=4, max_length=14_000_000),
    ]


class SchoolFactSourceResponse(_StrictHttpModel):
    source_ref: OpaqueRef
    source_sha256: Sha256Hex
    expires_at: AwareDatetime


class SchoolFactExcelPreflightRequest(_StrictHttpModel):
    source_ref: OpaqueRef
    base_version: int = Field(strict=True, ge=1)


class SchoolFactAdminChangeRequest(_StrictHttpModel):
    """HTTP 管理通道输入；显式重启由 Canonical 与身份范围共同约束。"""

    schema_version: Literal[1, 4] = 1
    operation: Literal["upsert", "end", "revoke", "reactivate"]
    fact_kind: str
    source_key: str
    expected_version: int
    effective_at: AwareDatetime = Field(strict=False)
    content: CanonicalFactContentV4 | CanonicalFactContentV1 | None


class SchoolFactAdminPreflightRequest(_StrictHttpModel):
    base_version: int = Field(strict=True, ge=1)
    changes: list[SchoolFactAdminChangeRequest] = Field(
        min_length=1,
        max_length=MAX_SCHOOL_FACT_CHANGES,
    )


class SchoolFactAdaptedSourceRequest(_StrictHttpModel):
    source_slot: NeutralSourceCode
    source_ref: OpaqueRef


class SchoolFactAdaptedPreflightRequest(_StrictHttpModel):
    sources: list[SchoolFactAdaptedSourceRequest] = Field(min_length=1, max_length=8)
    mapping_version: Annotated[
        str,
        StringConstraints(strict=True, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$"),
    ]
    base_version: int = Field(strict=True, ge=1)


class SchoolFactPackageSourceRequest(_StrictHttpModel):
    source_slot: NeutralSourceCode
    source_ref: OpaqueRef


class SchoolFactPackagePreflightRequest(_StrictHttpModel):
    manifest: FullScopeInputManifestV5
    sources: tuple[SchoolFactPackageSourceRequest, ...] = Field(min_length=1, max_length=32)

    @field_validator("manifest", mode="before")
    @classmethod
    def parse_strict_versioned_manifest(cls, value: object) -> FullScopeInputManifestV5:
        if isinstance(value, FullScopeInputManifestV5):
            return value
        if type(value) is not dict:
            raise ValueError("学校事实版本化输入清单无效。")
        return FullScopeInputManifestV5.model_validate_json(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        )

    @field_validator("sources", mode="before")
    @classmethod
    def parse_source_refs(cls, value: object) -> tuple[object, ...]:
        if type(value) is not list:
            raise ValueError("学校事实来源引用集合无效。")
        return tuple(value)


class SchoolFactSelectedFormInput(_StrictHttpModel):
    family: Literal["directory", "family", "education", "residence"]
    request: dict[str, JsonValue]


class SchoolFactSelectedPreflightRequest(_StrictHttpModel):
    items: list[SchoolFactSelectedFormInput] = Field(min_length=1, max_length=32)


class SchoolFactIssueResponse(_StrictHttpModel):
    source_slot: str
    sheet_code: str
    row_number: int
    column_code: str
    error_code: str


class SchoolFactFieldDiffResponse(_StrictHttpModel):
    field_code: str
    new_value: JsonValue | tuple[str, ...]


class SchoolFactSegmentSummaryResponse(_StrictHttpModel):
    effective_at: AwareDatetime
    valid_to: AwareDatetime | None = None
    cause_source_key: str | None = None
    cause_version: int | None = None
    fields: tuple[SchoolFactFieldDiffResponse, ...]


class SchoolFactChangeSummaryResponse(_StrictHttpModel):
    operation: FactOperation
    fact_kind: str
    source_key: str
    expected_version: int
    sheet_code: str
    row_number: int
    fields: tuple[SchoolFactFieldDiffResponse, ...]
    effective_at: AwareDatetime | None = None
    replacement_segments: tuple[SchoolFactSegmentSummaryResponse, ...] = ()
    correction_reason: str | None = None
    cause_source_key: str | None = None
    cause_version: int | None = None


class SchoolFactStatusDecisionResponse(_StrictHttpModel):
    source_slot: str
    sheet_code: str
    row_number: int
    field_code: str
    original_status_value: str
    standard_value: str
    mapping_version: str

    @field_validator("original_status_value")
    @classmethod
    def _validate_original_status_value(cls, value: str) -> str:
        return require_safe_status_original_value(value)


class SchoolFactPlanResponse(_StrictHttpModel):
    plan_ref: OpaqueRef
    plan_sha256: Sha256Hex
    is_clean: bool
    issues: tuple[SchoolFactIssueResponse, ...] = Field(max_length=MAX_SCHOOL_FACT_PREFLIGHT_ISSUES)
    changes: tuple[SchoolFactChangeSummaryResponse, ...] = Field(max_length=MAX_SCHOOL_FACT_CHANGES)
    status_decisions: tuple[SchoolFactStatusDecisionResponse, ...] = Field(
        max_length=MAX_SCHOOL_FACT_STATUS_DECISIONS
    )
    replacement_preview: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=50_000)
    preview_status: Literal["complete", "incomplete", "unavailable"] = "unavailable"
    preview_issues: tuple[str, ...] = ()
    change_preview: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=50_000)


class SchoolFactConfirmationResponse(_StrictHttpModel):
    plan_ref: OpaqueRef
    plan_sha256: Sha256Hex
    credential: OpaqueRef
    expires_at: AwareDatetime


class SchoolFactConfirmRequest(_StrictHttpModel):
    credential: OpaqueRef


class SchoolFactApplyResponse(_StrictHttpModel):
    plan_ref: OpaqueRef
    batch_id: str
    target_version: int
    changed_count: int
    replayed: bool
    cleanup_pending: bool


class SchoolFactResultResponse(_StrictHttpModel):
    plan_ref: OpaqueRef
    state: Literal["pending", "applied"]
    batch_id: str | None
    target_version: int | None
    changed_count: int | None
