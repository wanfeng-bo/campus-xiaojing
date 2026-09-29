# pyright: strict

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Self, cast
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BeforeValidator,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

from app.platform.contracts import FrozenContractModel
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    Sha256Hex,
)
from app.platform.school_facts.ports import (
    SchoolFactApplyResult,
    SchoolFactChangeProvenance,
    SchoolFactOwnOriginalResult,
    SchoolFactOwnOriginalSegment,
    SchoolFactOwnResult,
    SchoolFactOwnResultPage,
    SchoolFactOwnResultTarget,
    SchoolFactPolicyTextAttachment,
    SchoolFactRowProvenance,
    SchoolFactSourceArtifact,
    validate_own_original_selection,
)
from app.platform.school_facts.replacement_contracts import (
    MAX_PACKAGE_PROVENANCE_BYTES,
    MAX_REPLACEMENT_AUDIT_ARTIFACTS,
    PackageReplacementTarget,
)

EXECUTOR_AUTH_HEADER = "X-School-Fact-Executor-Key"


def _parse_uuid_transport(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise ValueError("跨进程 UUID 无效。") from exc
    raise ValueError("跨进程 UUID 无效。")


SchoolIdTransport = Annotated[UUID, BeforeValidator(_parse_uuid_transport)]
ContextToken = Annotated[
    str,
    StringConstraints(strict=True, min_length=80, max_length=4096, pattern=r"^[A-Za-z0-9_.-]+$"),
]
SessionBinding = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=2, max_length=256),
]
Credential = Annotated[
    str,
    StringConstraints(strict=True, min_length=40, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
]


class SchoolFactExecutorSourceArtifact(FrozenContractModel):
    logical_source_code: str
    source_sha256: Sha256Hex
    size_bytes: int = Field(strict=True, ge=1, le=10 * 1024 * 1024)

    def to_domain(self) -> SchoolFactSourceArtifact:
        return SchoolFactSourceArtifact(
            logical_source_code=self.logical_source_code,
            source_sha256=self.source_sha256,
            size_bytes=self.size_bytes,
        )


class SchoolFactExecutorRowProvenance(FrozenContractModel):
    fact_kind: str
    fact_source_key: str
    raw_source_sha256: Sha256Hex
    source_slot: str
    source_sheet_code: str
    source_row_number: int = Field(strict=True, ge=2, le=10_001)
    target_field_codes: tuple[str, ...]
    source_field_codes: tuple[str, ...]

    def to_domain(self) -> SchoolFactRowProvenance:
        return SchoolFactRowProvenance(
            fact_kind=self.fact_kind,
            fact_source_key=self.fact_source_key,
            raw_source_sha256=self.raw_source_sha256,
            source_slot=self.source_slot,
            source_sheet_code=self.source_sheet_code,
            source_row_number=self.source_row_number,
            target_field_codes=self.target_field_codes,
            source_field_codes=self.source_field_codes,
        )


class SchoolFactExecutorPolicyTextAttachment(FrozenContractModel):
    source_slot: str
    source_sha256: Sha256Hex
    fact_source_key: str
    field_code: Literal["content_text"] = "content_text"

    def to_domain(self) -> SchoolFactPolicyTextAttachment:
        return SchoolFactPolicyTextAttachment(
            source_slot=self.source_slot,
            source_sha256=self.source_sha256,
            fact_source_key=self.fact_source_key,
            field_code=self.field_code,
        )


class SchoolFactExecutorProvenance(FrozenContractModel):
    channel: Literal["admin", "excel", "package"]
    raw_source_sha256: Sha256Hex
    template_version: str
    mapping_version: str
    mapping_config_sha256: Sha256Hex
    source_size_bytes: int | None = Field(
        default=None, strict=True, ge=1, le=MAX_PACKAGE_PROVENANCE_BYTES
    )
    worksheet_names: tuple[str, ...] = ()
    source_manifest: tuple[SchoolFactExecutorSourceArtifact, ...] = Field(
        default=(), max_length=32 + MAX_REPLACEMENT_AUDIT_ARTIFACTS
    )
    row_lineage: tuple[SchoolFactExecutorRowProvenance, ...] = Field(default=(), max_length=50_000)
    policy_attachments: tuple[SchoolFactExecutorPolicyTextAttachment, ...] = Field(
        default=(), max_length=32
    )
    result_locator_sha256: Sha256Hex | None = None
    replacement_targets: tuple[PackageReplacementTarget, ...] = Field(default=(), max_length=50_000)

    @classmethod
    def from_domain(cls, value: SchoolFactChangeProvenance) -> SchoolFactExecutorProvenance:
        return cls(
            channel=value.channel,
            raw_source_sha256=value.raw_source_sha256,
            template_version=value.template_version,
            mapping_version=value.mapping_version,
            mapping_config_sha256=value.mapping_config_sha256,
            source_size_bytes=value.source_size_bytes,
            worksheet_names=value.worksheet_names,
            source_manifest=tuple(
                SchoolFactExecutorSourceArtifact(
                    logical_source_code=item.logical_source_code,
                    source_sha256=item.source_sha256,
                    size_bytes=item.size_bytes,
                )
                for item in value.source_manifest
            ),
            row_lineage=tuple(
                SchoolFactExecutorRowProvenance(
                    fact_kind=item.fact_kind,
                    fact_source_key=item.fact_source_key,
                    raw_source_sha256=item.raw_source_sha256,
                    source_slot=item.source_slot,
                    source_sheet_code=item.source_sheet_code,
                    source_row_number=item.source_row_number,
                    target_field_codes=item.target_field_codes,
                    source_field_codes=item.source_field_codes,
                )
                for item in value.row_lineage
            ),
            policy_attachments=tuple(
                SchoolFactExecutorPolicyTextAttachment(
                    source_slot=item.source_slot,
                    source_sha256=item.source_sha256,
                    fact_source_key=item.fact_source_key,
                    field_code=item.field_code,
                )
                for item in value.policy_attachments
            ),
            result_locator_sha256=value.result_locator_sha256,
            replacement_targets=value.replacement_targets,
        )

    def to_domain(self) -> SchoolFactChangeProvenance:
        return SchoolFactChangeProvenance(
            channel=self.channel,
            raw_source_sha256=self.raw_source_sha256,
            template_version=self.template_version,
            mapping_version=self.mapping_version,
            mapping_config_sha256=self.mapping_config_sha256,
            source_size_bytes=self.source_size_bytes,
            worksheet_names=self.worksheet_names,
            source_manifest=tuple(item.to_domain() for item in self.source_manifest),
            row_lineage=tuple(item.to_domain() for item in self.row_lineage),
            policy_attachments=tuple(item.to_domain() for item in self.policy_attachments),
            result_locator_sha256=self.result_locator_sha256,
            replacement_targets=self.replacement_targets,
        )


class _SchoolFactExecutorChangeSetRequest(FrozenContractModel):
    change_set: CanonicalFactChangeSetV1

    @field_validator("change_set", mode="before")
    @classmethod
    def parse_canonical_json_transport(cls, value: object) -> CanonicalFactChangeSetV1:
        if isinstance(value, CanonicalFactChangeSetV1):
            return value
        try:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return CanonicalFactChangeSetV1.model_validate_json(encoded)
        except (TypeError, ValueError) as exc:
            raise ValueError("跨进程 Canonical change set 无效。") from exc


class SchoolFactExecutorPreflightRequest(_SchoolFactExecutorChangeSetRequest):
    school_id: SchoolIdTransport
    change_set: CanonicalFactChangeSetV1
    context_token: ContextToken
    session_binding: SessionBinding


class SchoolFactExecutorPrepareRequest(_SchoolFactExecutorChangeSetRequest):
    plan_sha256: Sha256Hex
    context_token: ContextToken
    session_binding: SessionBinding
    provenance: SchoolFactExecutorProvenance | None = None

    @field_validator("provenance", mode="before")
    @classmethod
    def parse_provenance_json_transport(cls, value: object) -> SchoolFactExecutorProvenance | None:
        if value is None or isinstance(value, SchoolFactExecutorProvenance):
            return value
        try:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return SchoolFactExecutorProvenance.model_validate_json(encoded)
        except (TypeError, ValueError) as exc:
            raise ValueError("跨进程来源血缘无效。") from exc


class SchoolFactExecutorConfirmRequest(_SchoolFactExecutorChangeSetRequest):
    plan_sha256: Sha256Hex
    credential: Credential
    context_token: ContextToken
    session_binding: SessionBinding


class SchoolFactExecutorDigestResultRequest(FrozenContractModel):
    lookup_kind: Literal["digest"]
    school_id: SchoolIdTransport
    source_sha256: Sha256Hex
    plan_sha256: Sha256Hex
    context_token: ContextToken
    session_binding: SessionBinding


class SchoolFactExecutorLocatorResultRequest(FrozenContractModel):
    lookup_kind: Literal["locator"]
    school_id: SchoolIdTransport
    actor_user_id: int = Field(strict=True, ge=1)
    context_token: ContextToken
    session_binding: SessionBinding
    locator_sha256: Sha256Hex


type SchoolFactExecutorResultRequest = Annotated[
    SchoolFactExecutorDigestResultRequest | SchoolFactExecutorLocatorResultRequest,
    Field(discriminator="lookup_kind"),
]


class SchoolFactExecutorApplyResponse(FrozenContractModel):
    batch_id: UUID
    target_version: int = Field(strict=True, ge=1)
    changed_count: int = Field(strict=True, ge=0, le=50_000)
    replayed: bool


class SchoolFactExecutorResultResponse(FrozenContractModel):
    found: bool
    result: SchoolFactExecutorApplyResponse | None

    @model_validator(mode="after")
    def require_found_result_consistency(self) -> Self:
        if self.found != (self.result is not None):
            raise ValueError("Writer Executor result envelope is inconsistent.")
        return self


class SchoolFactExecutorOwnResultListRequest(FrozenContractModel):
    context_token: ContextToken
    session_binding: SessionBinding
    limit: int = Field(default=20, strict=True, ge=1, le=20)


class SchoolFactExecutorOwnResultLocatorRequest(FrozenContractModel):
    context_token: ContextToken
    session_binding: SessionBinding
    locator_sha256: Sha256Hex
    original_fields: dict[str, tuple[str, ...]] | None = None
    target_offset: int = Field(default=0, strict=True, ge=0)
    target_limit: int = Field(default=3, strict=True, ge=1, le=3)

    @field_validator("original_fields", mode="before")
    @classmethod
    def json_field_arrays(cls, value: Any) -> Any:
        if type(value) is dict:
            mapping = cast(dict[str, object], value)
            return {
                key: tuple(cast(list[object], items)) if type(items) is list else items
                for key, items in mapping.items()
            }
        return value

    @model_validator(mode="after")
    def finite_original_fields(self) -> Self:
        validate_own_original_selection(self.original_fields, self.target_offset, self.target_limit)
        return self

    @model_serializer(mode="wrap")
    def legacy_request(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = cast(dict[str, Any], handler(self))
        if self.original_fields is None:
            for key in ("original_fields", "target_offset", "target_limit"):
                data.pop(key, None)
        return data


class SchoolFactExecutorOwnOriginalSegment(FrozenContractModel):
    effective_at: AwareDatetime
    valid_to: AwareDatetime | None
    content: dict[str, str]


class SchoolFactExecutorOwnOriginalResult(FrozenContractModel):
    status: Literal["available", "denied", "unsupported", "unavailable"]
    target_version: int = Field(strict=True, ge=1)
    operation: Literal["upsert", "end", "revoke", "reactivate", "correct", "cancel"]
    field_names: tuple[str, ...] = Field(max_length=3)
    segments: tuple[SchoolFactExecutorOwnOriginalSegment, ...] = Field(default=(), max_length=128)
    before_segments: tuple[SchoolFactExecutorOwnOriginalSegment, ...] = Field(
        default=(), max_length=128
    )
    field_changes: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def complete_projection(self) -> Self:
        from app.platform.school_facts.ports import OWN_ORIGINAL_FIELDS

        if not set(self.field_names) <= OWN_ORIGINAL_FIELDS:
            raise ValueError("Original receipt field projection is unregistered.")
        if self.status != "available" and (
            self.segments or self.before_segments or self.field_changes
        ):
            raise ValueError("Unavailable original values cannot contain segments.")
        for segments in (self.segments, self.before_segments):
            previous = None
            for segment in segments:
                if set(segment.content) != set(self.field_names):
                    raise ValueError("Original receipt field projection is incomplete.")
                if segment.valid_to is not None and segment.valid_to <= segment.effective_at:
                    raise ValueError("Original receipt interval is invalid.")
                if previous is not None and (
                    previous.valid_to is None or previous.valid_to > segment.effective_at
                ):
                    raise ValueError("Original receipt intervals overlap.")
                previous = segment
        if self.field_changes != bool(self.segments or self.before_segments):
            raise ValueError("Original receipt changed fields must match their intervals.")
        return self

    def to_domain(self) -> SchoolFactOwnOriginalResult:
        return SchoolFactOwnOriginalResult(
            self.status,
            self.target_version,
            self.operation,
            self.field_names,
            tuple(
                SchoolFactOwnOriginalSegment(s.effective_at, s.valid_to, dict(s.content))
                for s in self.segments
            ),
            tuple(
                SchoolFactOwnOriginalSegment(s.effective_at, s.valid_to, dict(s.content))
                for s in self.before_segments
            ),
            self.field_changes,
        )


class SchoolFactExecutorOwnResultTarget(FrozenContractModel):
    fact_kind: str = Field(min_length=1, max_length=64, pattern=r"^[a-z_]+$")
    source_key: str = Field(min_length=1, max_length=255)
    operation: Literal["upsert", "end", "revoke", "reactivate", "correct", "cancel"]
    target_version: int = Field(strict=True, ge=1)
    creation_receipt: bool = Field(default=False, strict=True)
    original_result: SchoolFactExecutorOwnOriginalResult | None = None

    @model_serializer(mode="wrap")
    def legacy_target(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = cast(dict[str, Any], handler(self))
        if self.original_result is None:
            data.pop("original_result", None)
        return data

    @model_validator(mode="after")
    def require_registered_temporal_operation(self) -> Self:
        if self.original_result is not None and (
            self.original_result.target_version != self.target_version
            or self.original_result.operation != self.operation
            or (self.creation_receipt and self.original_result.status == "available")
        ):
            raise ValueError("Original values must remain bound to their authorized receipt.")
        if self.creation_receipt and (self.operation != "upsert" or self.target_version != 1):
            raise ValueError("Creation receipt requires an original created target.")
        from app.domains.students.education_contracts import EDUCATION_FACT_KINDS

        if self.operation in {"correct", "cancel"} and not (
            self.fact_kind in EDUCATION_FACT_KINDS
            or (self.fact_kind == "student_school_relationship" and self.operation == "correct")
        ):
            raise ValueError("Temporal receipt operation is not registered for this fact kind.")
        return self

    def to_domain(self) -> SchoolFactOwnResultTarget:
        return SchoolFactOwnResultTarget(
            self.fact_kind,
            self.source_key,
            self.operation,
            self.target_version,
            self.creation_receipt,
            self.original_result.to_domain() if self.original_result is not None else None,
        )


class SchoolFactExecutorOwnResult(FrozenContractModel):
    locator_sha256: Sha256Hex
    confirmed_at: AwareDatetime
    result: SchoolFactExecutorApplyResponse
    targets: tuple[SchoolFactExecutorOwnResultTarget, ...] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def require_complete_applied_targets(self) -> Self:
        if (
            not self.result.replayed
            or self.result.changed_count != len(self.targets)
            or len({(target.fact_kind, target.source_key) for target in self.targets})
            != len(self.targets)
        ):
            raise ValueError("Own receipt result must describe one complete applied batch.")
        return self

    @classmethod
    def from_domain(cls, value: SchoolFactOwnResult) -> Self:
        return cls.model_validate(
            {
                "locator_sha256": value.locator_sha256,
                "confirmed_at": value.confirmed_at,
                "result": SchoolFactExecutorApplyResponse(
                    batch_id=value.result.batch_id,
                    target_version=value.result.target_version,
                    changed_count=value.result.changed_count,
                    replayed=value.result.replayed,
                ),
                "targets": tuple(
                    {
                        "fact_kind": target.fact_kind,
                        "source_key": target.source_key,
                        "operation": target.operation,
                        "target_version": target.target_version,
                        "creation_receipt": target.creation_receipt,
                        **(
                            {
                                "original_result": {
                                    "status": target.original_result.status,
                                    "target_version": target.original_result.target_version,
                                    "operation": target.original_result.operation,
                                    "field_names": target.original_result.field_names,
                                    "segments": tuple(
                                        {
                                            "effective_at": s.effective_at,
                                            "valid_to": s.valid_to,
                                            "content": s.content,
                                        }
                                        for s in target.original_result.segments
                                    ),
                                    "before_segments": tuple(
                                        {
                                            "effective_at": s.effective_at,
                                            "valid_to": s.valid_to,
                                            "content": s.content,
                                        }
                                        for s in target.original_result.before_segments
                                    ),
                                    "field_changes": target.original_result.field_changes,
                                }
                            }
                            if target.original_result is not None
                            else {}
                        ),
                    }
                    for target in value.targets
                ),
            }
        )

    def to_domain(self) -> SchoolFactOwnResult:
        return SchoolFactOwnResult(
            self.locator_sha256,
            self.confirmed_at,
            SchoolFactApplyResult(
                self.result.batch_id,
                self.result.target_version,
                self.result.changed_count,
                self.result.replayed,
            ),
            tuple(target.to_domain() for target in self.targets),
        )


class SchoolFactExecutorOwnResultPage(FrozenContractModel):
    items: tuple[SchoolFactExecutorOwnResult, ...] = Field(max_length=20)
    has_more: bool

    def to_domain(self) -> SchoolFactOwnResultPage:
        return SchoolFactOwnResultPage(
            tuple(item.to_domain() for item in self.items), self.has_more
        )


class SchoolFactExecutorOwnResultLookupResponse(FrozenContractModel):
    found: bool
    item: SchoolFactExecutorOwnResult | None

    @model_validator(mode="after")
    def require_found_consistency(self) -> Self:
        if self.found != (self.item is not None):
            raise ValueError("Own receipt lookup is inconsistent.")
        return self
