"""F7 multi-source candidate normalization; never grants access or executes a fact change.

The old v2/v4 workbook readers keep their original complete-sheet semantics.  Their
validated changes, and changes prepared by other fact-family adapters, enter this
versioned package only after each source has been read through its protected path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from hashlib import sha256
from io import BytesIO
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, ValidationError, field_validator, model_validator

from app.platform.contracts import FrozenContractModel
from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from app.platform.master_data_imports.school_facts_excel import (
    SchoolFactWorkbookParser,
    SchoolFactWorkbookPlan,
)
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    NonBlankText,
    SafeCode,
    SchoolFactKind,
    Sha256Hex,
    StableSourceKey,
    require_neutral_lineage_code,
)
from app.platform.school_facts.package_integrity import full_scope_plan_sha256
from app.platform.school_facts.ports import (
    SchoolFactPolicyTextAttachment,
    SchoolFactRowProvenance,
    SchoolFactSourceArtifact,
)
from app.platform.school_facts.replacement_contracts import (
    MAX_REPLACEMENT_AUDIT_BYTES,
    REPLACEMENT_AUDIT_CHUNK,
    REPLACEMENT_AUDIT_PREFIX,
    PackageReplacementTarget,
)

FULL_SCOPE_INPUT_VERSION = "school_facts_v5"
FULL_SCOPE_FACT_SOURCE_VERSION = "school_facts_source_v5"
FULL_SCOPE_JSON_MAPPING_SHA256 = sha256(
    b"school_facts_source_v5:canonical_fact_change_v4:server_origin_v1"
).hexdigest()
MAX_FULL_SCOPE_BYTES = 10 * 1024 * 1024
MAX_FULL_SCOPE_SOURCES = 32
MAX_FULL_SCOPE_CHANGES = 50_000


class FullScopeInputRejected(ValueError):
    """A safe, stable issue code without source text or private file details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FullScopeReplacementScopeV5(FrozenContractModel):
    """Requested complete-replacement *scope*, not a claim that its snapshot is complete."""

    fact_kind: SchoolFactKind
    source_keys: tuple[StableSourceKey, ...] = Field(min_length=1, max_length=50_000)
    effective_at: AwareDatetime

    @model_validator(mode="after")
    def unique_objects(self) -> Self:
        if len(set(self.source_keys)) != len(self.source_keys):
            raise ValueError("Replacement scope contains a duplicate object.")
        return self


class FullScopeDomainOptionsV5(FrozenContractModel):
    """Explicit selected references and date from the existing maintenance form."""

    effective_at: AwareDatetime
    fact_kind: SchoolFactKind | None = None
    target_refs: tuple[str, ...] = Field(min_length=1, max_length=300)
    reason: NonBlankText


class FullScopeSourceV5(FrozenContractModel):
    """Server-bound neutral source slot; the original bytes are not persisted here."""

    logical_source_code: SafeCode
    source_sha256: Sha256Hex
    size_bytes: int = Field(strict=True, ge=1, le=MAX_FULL_SCOPE_BYTES)
    mapping_version: SafeCode
    mapping_config_sha256: Sha256Hex
    responsibility_reference: NonBlankText
    purpose: Literal["facts", "policy_text"] = "facts"
    attached_policy_source_key: StableSourceKey | None = None
    fact_kinds: tuple[SchoolFactKind, ...] = Field(max_length=53)
    domain_options: FullScopeDomainOptionsV5 | None = None

    @field_validator("logical_source_code")
    @classmethod
    def neutral_slot(cls, value: str) -> str:
        return require_neutral_lineage_code(value)

    @model_validator(mode="after")
    def unique_kinds(self) -> Self:
        if len(set(self.fact_kinds)) != len(self.fact_kinds):
            raise ValueError("Source fact families must be unique.")
        if self.purpose == "policy_text":
            if self.fact_kinds or self.attached_policy_source_key is None:
                raise ValueError("Policy text source requires one exact policy target.")
        elif not self.fact_kinds or self.attached_policy_source_key is not None:
            raise ValueError("Fact source requires declared kinds and no attachment target.")
        return self


class FullScopeInputManifestV5(FrozenContractModel):
    """Explicit F7 input scope. Omission never asserts absence or complete coverage."""

    input_version: Literal["school_facts_v5"]
    school_id: UUID
    base_version: int = Field(strict=True, ge=1)
    mode: Literal["incremental", "replace"] = "incremental"
    coverage_status: Literal["unknown"] = "unknown"
    replacement_scopes: tuple[FullScopeReplacementScopeV5, ...] = Field(default=(), max_length=32)
    sources: tuple[FullScopeSourceV5, ...] = Field(min_length=1, max_length=MAX_FULL_SCOPE_SOURCES)

    @model_validator(mode="after")
    def bound_exact_scope_and_capacity(self) -> Self:
        if self.mode == "replace" and not self.replacement_scopes:
            raise ValueError("Complete replacement requires an explicit selected scope.")
        if self.mode == "incremental" and self.replacement_scopes:
            raise ValueError("Incremental input cannot imply replacement.")
        if len({item.logical_source_code for item in self.sources}) != len(self.sources):
            raise ValueError("Logical source slots must be unique.")
        if sum(item.size_bytes for item in self.sources) > MAX_FULL_SCOPE_BYTES:
            raise ValueError("Full input package exceeds its total source-byte limit.")
        declared_kinds = {kind for source in self.sources for kind in source.fact_kinds}
        if any(scope.fact_kind not in declared_kinds for scope in self.replacement_scopes):
            raise ValueError("Replacement scope must belong to a declared fact family.")
        selected = [
            (scope.fact_kind, source_key)
            for scope in self.replacement_scopes
            for source_key in scope.source_keys
        ]
        if len(set(selected)) != len(selected) or len(selected) > MAX_FULL_SCOPE_CHANGES:
            raise ValueError("Replacement objects must be unique and bounded.")
        return self


@dataclass(frozen=True, slots=True)
class FullScopeSourcePart:
    """One previously parsed source, including bytes only for integrity checking."""

    logical_source_code: str
    original_bytes: bytes = field(repr=False)
    changes: tuple[CanonicalFactChangeV1, ...]
    row_lineage: tuple[SchoolFactRowProvenance, ...]
    domain_review: object | None = field(default=None, repr=False)


def parse_full_scope_fact_json_source(
    source: FullScopeSourceV5, original_bytes: bytes, *, allow_empty: bool = False
) -> FullScopeSourcePart:
    """Parse a private engineering interchange, not an operator-friendly import UI.

    The server assigns the causal row coordinates. No source can submit its own
    already-signed plan, alternate mapping or arbitrary database command.
    """

    if (
        source.purpose != "facts"
        or source.mapping_version != FULL_SCOPE_FACT_SOURCE_VERSION
        or source.mapping_config_sha256 != FULL_SCOPE_JSON_MAPPING_SHA256
    ):
        raise FullScopeInputRejected("SOURCE_MAPPING_NOT_REGISTERED")
    if (
        len(original_bytes) != source.size_bytes
        or sha256(original_bytes).hexdigest() != source.source_sha256
    ):
        raise FullScopeInputRejected("SOURCE_CONTENT_MISMATCH")
    try:
        decoded = json.loads(original_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FullScopeInputRejected("SOURCE_FORMAT_INVALID") from None
    if (
        type(decoded) is not dict
        or set(decoded) != {"input_version", "changes"}
        or decoded["input_version"] != FULL_SCOPE_FACT_SOURCE_VERSION
        or type(decoded["changes"]) is not list
        or not (0 if allow_empty else 1) <= len(decoded["changes"]) <= 10_000
    ):
        raise FullScopeInputRejected("SOURCE_FORMAT_INVALID")
    changes: list[CanonicalFactChangeV1] = []
    lineage: list[SchoolFactRowProvenance] = []
    for row_number, raw in enumerate(decoded["changes"], start=2):
        if type(raw) is not dict or {"schema_version", "origin", "source_sha256"} & set(raw):
            raise FullScopeInputRejected("SOURCE_CHANGE_INVALID")
        segments = raw.get("replacement_segments")
        if segments is not None and type(segments) is not list:
            raise FullScopeInputRejected("SOURCE_CHANGE_INVALID")
        # Causal restoration belongs to a trusted domain builder. An uploaded
        # interchange cannot claim another formal action as its execution cause.
        if {"cause_source_key", "cause_version"} & set(raw) or any(
            type(segment) is dict and {"cause_source_key", "cause_version"} & set(segment)
            for segment in (segments or ())
        ):
            raise FullScopeInputRejected("DOMAIN_CAUSAL_PROVENANCE_REQUIRED")
        try:
            change = CanonicalFactChangeV1.model_validate_json(
                json.dumps(
                    {
                        **raw,
                        "schema_version": 4,
                        "origin": {"sheet_code": "f7_source", "row_number": row_number},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        except ValidationError:
            raise FullScopeInputRejected("SOURCE_CHANGE_INVALID") from None
        if change.fact_kind not in source.fact_kinds:
            raise FullScopeInputRejected("FACT_KIND_UNDECLARED")
        # Field lineage is a safe structural coordinate, never raw cell content.
        field_codes = tuple(
            sorted(raw["content"])
                if type(raw.get("content")) is dict and raw["content"]
            else ("operation",)
        )
        try:
            lineage.append(
                SchoolFactRowProvenance(
                    fact_kind=change.fact_kind,
                    fact_source_key=change.source_key,
                    raw_source_sha256=source.source_sha256,
                    source_slot=source.logical_source_code,
                    source_sheet_code="f7_source",
                    source_row_number=row_number,
                    target_field_codes=field_codes,
                    source_field_codes=field_codes,
                )
            )
        except ValueError:
            raise FullScopeInputRejected("SOURCE_FIELD_LINEAGE_INVALID") from None
        changes.append(change)
    return FullScopeSourcePart(
        logical_source_code=source.logical_source_code,
        original_bytes=original_bytes,
        changes=tuple(changes),
        row_lineage=tuple(lineage),
    )


def parse_full_scope_workbook_source(
    declared: FullScopeSourceV5, original_bytes: bytes, *,
    source: ProtectedImportSourceRef, base_version: int,
) -> FullScopeSourcePart:
    """Reuse registered v2/v4 parser and status mapping without relaxing old sheets.

    Empty sheets still mean no submitted changes, never a request to remove facts.
    Stable references and row provenance are retained; F7 only renames the source slot.
    """
    parsed = SchoolFactWorkbookParser().parse(
        BytesIO(original_bytes), source=source, base_version=base_version
    )
    if (
        declared.purpose != "facts"
        or declared.mapping_version != parsed.mapping_version
        or declared.mapping_config_sha256 != parsed.mapping_config_sha256
        or declared.source_sha256 != parsed.raw_source_sha256
        or declared.size_bytes != len(original_bytes)
        or any(change.fact_kind not in declared.fact_kinds for change in parsed.change_set.changes)
    ):
        raise FullScopeInputRejected("SOURCE_MAPPING_NOT_REGISTERED")
    return FullScopeSourcePart(
        logical_source_code=declared.logical_source_code,
        original_bytes=original_bytes,
        changes=parsed.change_set.changes,
        row_lineage=tuple(
            replace(row, source_slot=declared.logical_source_code) for row in parsed.row_lineage
        ),
    )


@dataclass(frozen=True, slots=True)
class FullScopePackagePlan:
    input_version: Literal["school_facts_v5"]
    school_id: UUID
    mode: Literal["incremental", "replace"]
    replacement_scopes: tuple[FullScopeReplacementScopeV5, ...]
    coverage_status: Literal["unknown"]
    raw_source_sha256: str
    change_set: CanonicalFactChangeSetV1
    source_manifest: tuple[SchoolFactSourceArtifact, ...]
    row_lineage: tuple[SchoolFactRowProvenance, ...]
    attachment_bindings: tuple[FullScopeAttachmentBinding, ...]
    replacement_targets: tuple[PackageReplacementTarget, ...] = ()


@dataclass(frozen=True, slots=True)
class FullScopeAttachmentBinding:
    source_slot: str
    source_sha256: str
    fact_source_key: str
    field_code: Literal["content_text"] = "content_text"


def source_parts_from_workbook_plan(
    plan: SchoolFactWorkbookPlan, original_bytes_by_slot: Mapping[str, bytes]
) -> tuple[FullScopeSourcePart, ...]:
    """Carry verified v2/v4 workbook results forward without changing their parser.

    The caller retrieves original bytes through the protected source store.  A
    source-mapped workbook may contain several logical slots whose parsed rows
    are interleaved; this preserves each original row coordinate while grouping
    the slots in the declared order for the F7 package.
    """

    slots = tuple(item.logical_source_code for item in plan.source_manifest)
    if set(original_bytes_by_slot) != set(slots) or len(slots) != len(set(slots)):
        raise FullScopeInputRejected("SOURCE_SET_MISMATCH")
    if len(plan.change_set.changes) != len(plan.row_lineage):
        raise FullScopeInputRejected("SOURCE_LINEAGE_MISMATCH")
    grouped: dict[str, list[tuple[CanonicalFactChangeV1, SchoolFactRowProvenance]]] = {
        slot: [] for slot in slots
    }
    for change, lineage in zip(plan.change_set.changes, plan.row_lineage, strict=True):
        if lineage.source_slot not in grouped:
            raise FullScopeInputRejected("SOURCE_LINEAGE_MISMATCH")
        grouped[lineage.source_slot].append((change, lineage))
    return tuple(
        FullScopeSourcePart(
            logical_source_code=slot,
            original_bytes=original_bytes_by_slot[slot],
            changes=tuple(change for change, _origin in grouped[slot]),
            row_lineage=tuple(origin for _change, origin in grouped[slot]),
        )
        for slot in slots
    )


def require_full_scope_change_capacity(count: int) -> None:
    if not 1 <= count <= MAX_FULL_SCOPE_CHANGES:
        raise FullScopeInputRejected("CHANGE_COUNT_EXCEEDED")


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class FullScopePackageBuilder:
    """Revalidate mixed fact-family candidates as one schema-4 plan.

    This does not infer endings for omitted rows. In replacement mode the owner
    must first obtain an authorized complete snapshot and add every proposed end
    to the parts; final preflight must recheck that snapshot and its permissions.
    """

    def build(
        self,
        *,
        manifest: FullScopeInputManifestV5,
        parts: tuple[FullScopeSourcePart, ...],
        replacement_targets: tuple[PackageReplacementTarget, ...] = (),
        generated_changes: tuple[CanonicalFactChangeV1, ...] = (),
    ) -> FullScopePackagePlan:
        if tuple(part.logical_source_code for part in parts) != tuple(
            source.logical_source_code for source in manifest.sources
        ):
            raise FullScopeInputRejected("SOURCE_SET_MISMATCH")
        if any(s.logical_source_code.startswith("f7_replacement_") for s in manifest.sources):
            raise FullScopeInputRejected("RESERVED_SOURCE_SLOT")
        if manifest.mode != "replace" and (replacement_targets or generated_changes):
            raise FullScopeInputRejected("REPLACEMENT_MODE_REQUIRED")
        if manifest.mode == "replace" and replacement_targets:
            expected = {(s.fact_kind, key, s.effective_at)
                        for s in manifest.replacement_scopes for key in s.source_keys}
            if expected != {
                (t.fact_kind, t.source_key, t.effective_at) for t in replacement_targets
            }:
                raise FullScopeInputRejected("REPLACEMENT_SNAPSHOT_SET_MISMATCH")
        require_full_scope_change_capacity(sum(len(part.changes) for part in parts)
                                          + len(generated_changes))
        canonical: list[CanonicalFactChangeV1] = []
        lineage: list[SchoolFactRowProvenance] = []
        source_manifest: list[SchoolFactSourceArtifact] = []
        text_sources: list[tuple[FullScopeSourceV5, bytes]] = []
        for source_index, (source, part) in enumerate(
            zip(manifest.sources, parts, strict=True), start=1
        ):
            if (
                len(part.original_bytes) != source.size_bytes
                or sha256(part.original_bytes).hexdigest() != source.source_sha256
            ):
                raise FullScopeInputRejected("SOURCE_CONTENT_MISMATCH")
            if len(part.changes) != len(part.row_lineage):
                raise FullScopeInputRejected("SOURCE_LINEAGE_MISMATCH")
            source_manifest.append(
                SchoolFactSourceArtifact(
                    logical_source_code=source.logical_source_code,
                    source_sha256=source.source_sha256,
                    size_bytes=source.size_bytes,
                )
            )
            if source.purpose == "policy_text":
                if part.changes or part.row_lineage:
                    raise FullScopeInputRejected("POLICY_TEXT_CONTAINS_FACT_ROWS")
                text_sources.append((source, part.original_bytes))
                continue
            for change, origin in zip(part.changes, part.row_lineage, strict=True):
                if change.fact_kind not in source.fact_kinds:
                    raise FullScopeInputRejected("FACT_KIND_UNDECLARED")
                if (
                    origin.fact_kind != change.fact_kind
                    or origin.fact_source_key != change.source_key
                    or origin.raw_source_sha256 != source.source_sha256
                    or origin.source_slot != source.logical_source_code
                ):
                    raise FullScopeInputRejected("SOURCE_LINEAGE_MISMATCH")
                payload = change.model_dump(mode="json", exclude_unset=True)
                payload["schema_version"] = 4
                payload["origin"] = {
                    "sheet_code": f"f7_s{source_index}",
                    "row_number": len(canonical) + 2,
                }
                try:
                    canonical.append(
                        CanonicalFactChangeV1.model_validate_json(
                            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                        )
                    )
                except ValidationError:
                    raise FullScopeInputRejected("CANONICAL_NORMALIZATION_REJECTED") from None
                lineage.append(origin)
        selection_digest = _digest([t.model_dump(mode="json") for t in replacement_targets])
        for offset in range(0, len(generated_changes), REPLACEMENT_AUDIT_CHUNK):
            chunk = generated_changes[offset:offset + REPLACEMENT_AUDIT_CHUNK]
            source_slot = f"{REPLACEMENT_AUDIT_PREFIX}{offset // REPLACEMENT_AUDIT_CHUNK + 1}"
            # Explicitly server-derived evidence, never a fictitious workbook row.
            material = json.dumps({
                "selection_sha256": selection_digest,
                "generated_sha256": [_digest(c.model_dump(mode="json", exclude_unset=True))
                                     for c in chunk],
            }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            if len(material) > MAX_REPLACEMENT_AUDIT_BYTES:
                raise FullScopeInputRejected("REPLACEMENT_AUDIT_CAPACITY_EXCEEDED")
            digest = sha256(material).hexdigest()
            source_manifest.append(SchoolFactSourceArtifact(source_slot, digest, len(material)))
            for index, change in enumerate(chunk, 2):
                payload = change.model_dump(mode="json", exclude_unset=True)
                payload.update(schema_version=4, origin={"sheet_code": source_slot,
                                                         "row_number": index})
                canonical.append(CanonicalFactChangeV1.model_validate_json(json.dumps(payload)))
                lineage.append(SchoolFactRowProvenance(
                    fact_kind=change.fact_kind, fact_source_key=change.source_key,
                    raw_source_sha256=digest, source_slot=source_slot,
                    source_sheet_code=source_slot, source_row_number=index,
                    target_field_codes=("operation",), source_field_codes=("operation",),
                ))
        attachment_bindings: list[FullScopeAttachmentBinding] = []
        bound_policies: set[str] = set()
        for source, original_bytes in text_sources:
            key = source.attached_policy_source_key
            assert key is not None
            if key in bound_policies:
                raise FullScopeInputRejected("POLICY_TEXT_TARGET_DUPLICATE")
            bound_policies.add(key)
            matching = [
                change
                for change in canonical
                if change.fact_kind == "policy_version" and change.source_key == key
            ]
            try:
                text_value = original_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raise FullScopeInputRejected("POLICY_TEXT_ENCODING_INVALID") from None
            if (
                len(matching) != 1
                or matching[0].content is None
                or matching[0].content.content_text != text_value
            ):
                raise FullScopeInputRejected("POLICY_TEXT_MISMATCH")
            attachment_bindings.append(
                FullScopeAttachmentBinding(
                    source_slot=source.logical_source_code,
                    source_sha256=source.source_sha256,
                    fact_source_key=key,
                )
            )
        raw_digest = _digest(
            {
                "manifest": manifest.model_dump(mode="json"),
                "sources": [
                    {
                        "logical_source_code": item.logical_source_code,
                        "source_sha256": item.source_sha256,
                        "size_bytes": item.size_bytes,
                    }
                    for item in source_manifest
                ],
            }
        )
        plan_digest = full_scope_plan_sha256(
            raw_source_sha256=raw_digest,
            changes=tuple(canonical),
            row_lineage=tuple(lineage),
            policy_attachments=tuple(
                SchoolFactPolicyTextAttachment(
                    source_slot=item.source_slot,
                    source_sha256=item.source_sha256,
                    fact_source_key=item.fact_source_key,
                    field_code=item.field_code,
                )
                for item in attachment_bindings
            ),
            replacement_targets=replacement_targets,
        )
        try:
            change_set = CanonicalFactChangeSetV1(
                schema_version=4,
                source_sha256=plan_digest,
                base_version=manifest.base_version,
                changes=tuple(canonical),
            )
        except ValidationError:
            raise FullScopeInputRejected("CANONICAL_CHANGE_SET_REJECTED") from None
        return FullScopePackagePlan(
            input_version=FULL_SCOPE_INPUT_VERSION,
            school_id=manifest.school_id,
            mode=manifest.mode,
            replacement_scopes=manifest.replacement_scopes,
            coverage_status=manifest.coverage_status,
            raw_source_sha256=raw_digest,
            change_set=change_set,
            source_manifest=tuple(source_manifest),
            row_lineage=tuple(lineage),
            attachment_bindings=tuple(attachment_bindings),
            replacement_targets=replacement_targets,
        )
