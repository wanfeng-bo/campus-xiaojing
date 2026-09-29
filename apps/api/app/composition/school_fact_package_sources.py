"""Registered F5/F6 input purposes reuse their owned finite maintenance builders.

This adapter never executes, grants access, or trusts client canonical timelines.
Its source bytes remain in the existing owner/session-bound short-lived store.
"""

import json
from hashlib import sha256
from io import BytesIO
from uuid import UUID

from app.platform.master_data_imports.school_facts_package import (
    FullScopeInputRejected,
    FullScopeSourcePart,
    FullScopeSourceV5,
)
from app.platform.master_data_imports.school_facts_replacement import PreparedReplacementEnd
from app.platform.school_facts.ports import SchoolFactRowProvenance
from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates

DOMAIN_SOURCE_VERSIONS = frozenset(
    {"f7_teaching_xlsx_v1", "f7_residence_import_v1", "f7_selected_form_v1"}
)


class PackageDomainSourceAdapter:
    def __init__(self, *, education_resolver=None, residence_resolver=None, form_resolvers=None):
        self.education_resolver = education_resolver
        self.residence_resolver = residence_resolver
        self.form_resolvers = dict(form_resolvers or {})

    def prepare_form(self, *, actor, family, slot, request, effective_at):
        """The existing authorized form owns object selection; the server owns its manifest."""
        if family not in self.form_resolvers:
            raise FullScopeInputRejected("SOURCE_DOMAIN_ADAPTER_UNAVAILABLE")
        model, resolver = self.form_resolvers[family]
        values = dict(request)
        values["effective_at"] = values.get("effective_at") or effective_at.isoformat()
        payload = model.model_validate_json(json.dumps(values, ensure_ascii=False))
        raw = json.dumps(
            {
                "input_version": "f7_selected_form_v1",
                "family": family,
                "request": payload.model_dump(mode="json"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        digest = sha256(raw).hexdigest()
        resolved = resolver(
            actor=actor,
            payload=payload,
            request_id=UUID(digest[:32]),
            effective_at=payload.effective_at,
        )
        source = FullScopeSourceV5(
            logical_source_code=slot,
            source_sha256=digest,
            size_bytes=len(raw),
            mapping_version="f7_selected_form_v1",
            mapping_config_sha256=sha256(b"f7_selected_form_v1").hexdigest(),
            responsibility_reference="当前获准维护表单与正式变更依据",
            fact_kinds=tuple(dict.fromkeys(c.fact_kind for c in resolved.change_set.changes)),
        )
        return source, self._part(
            actor,
            source,
            raw,
            resolved,
            "selected_form",
            fallback=self._form_proof(resolver, payload, digest, resolved),
        )

    @staticmethod
    def _form_proof(resolver, payload, digest, original):
        # Older directory adapters supply no retained proof. Re-run their same
        # protected reader and compare the complete result; never invent a grant.
        def revalidate(actor):
            current = resolver(
                actor=actor,
                payload=payload,
                request_id=UUID(digest[:32]),
                effective_at=payload.effective_at,
            )
            if current.change_set != original.change_set or current.summary != original.summary:
                raise FullScopeInputRejected("SOURCE_DOMAIN_SNAPSHOT_CHANGED")
            if callable(current.summary_revalidate):
                current.summary_revalidate(actor)

        return revalidate

    def _part(self, actor, declared, raw, resolved, sheet, row_by_key=None, fallback=None):
        if any(c.fact_kind not in declared.fact_kinds for c in resolved.change_set.changes):
            raise FullScopeInputRejected("FACT_KIND_UNDECLARED")
        proof = resolved.summary_revalidate or fallback
        if not callable(proof):
            raise FullScopeInputRejected("SOURCE_DOMAIN_PROOF_REQUIRED")

        def revalidate():
            proof(actor)

        revalidate()
        lineage = []
        for change in resolved.change_set.changes:
            row_number = (
                (row_by_key.get(change.source_key) or row_by_key.get(change.cause_source_key))
                if row_by_key
                else 2
            )
            if row_number is None:
                raise FullScopeInputRejected("SOURCE_DOMAIN_CAUSAL_ROW_UNAVAILABLE")
            lineage.append(
                SchoolFactRowProvenance(
                    fact_kind=change.fact_kind,
                    fact_source_key=change.source_key,
                    raw_source_sha256=declared.source_sha256,
                    source_slot=declared.logical_source_code,
                    source_sheet_code=sheet,
                    source_row_number=row_number,
                    target_field_codes=("operation",),
                    source_field_codes=("source_key" if row_by_key else "request",),
                )
            )
        return FullScopeSourcePart(
            logical_source_code=declared.logical_source_code,
            original_bytes=raw,
            changes=resolved.change_set.changes,
            row_lineage=tuple(lineage),
            domain_review=PreparedReplacementEnd(
                resolved.change_set.changes,
                resolved.summary.model_dump(mode="json"),
                revalidate,
                resolved.change_set.base_version,
            ),
        )

    def parse(self, *, actor, declared, raw, base_version):
        if (
            declared.mapping_version not in DOMAIN_SOURCE_VERSIONS
            or declared.mapping_config_sha256
            != sha256(declared.mapping_version.encode()).hexdigest()
            or declared.purpose != "facts"
            or sha256(raw).hexdigest() != declared.source_sha256
            or len(raw) != declared.size_bytes
        ):
            raise FullScopeInputRejected("SOURCE_MAPPING_NOT_REGISTERED")
        row_by_key = None
        if declared.mapping_version == "f7_teaching_xlsx_v1":
            from app.composition.school_fact_education_product import EducationBatchInput
            from app.domains.students.teaching_excel import read_teaching_workbook

            options = declared.domain_options
            if options is None or self.education_resolver is None:
                raise FullScopeInputRejected("SOURCE_DOMAIN_OPTIONS_REQUIRED")
            kind = options.fact_kind or (
                declared.fact_kinds[0] if len(declared.fact_kinds) == 1 else None
            )
            if kind not in declared.fact_kinds:
                raise FullScopeInputRejected("SOURCE_DOMAIN_OPTIONS_REQUIRED")
            rows = read_teaching_workbook(raw, kind)
            ref = options.target_refs[0]
            coordinates = school_scope_reference_coordinates(ref)
            creation = ref.startswith("f4create:") or bool(
                coordinates and coordinates[0] in {"f4", "f5"} and coordinates[2])
            payload = EducationBatchInput.model_validate_json(
                json.dumps(
                    {
                        "fact_kind": kind,
                        "operation": "upsert",
                        "fields": {},
                        "target_ref": None if creation else ref,
                        "creation_ref": ref if creation else None,
                        "reason": options.reason,
                        "effective_at": options.effective_at.isoformat(),
                        "import_rows": rows,
                        "import_target_refs": options.target_refs,
                        "import_mode": "incremental",
                    },
                    default=str,
                )
            )
            # The original parser omits empty rows. Recover only their physical
            # coordinates after its validation, without changing import semantics.
            from openpyxl import load_workbook

            workbook = load_workbook(BytesIO(raw), read_only=True, data_only=False)
            try:
                source_rows = workbook["teaching"].iter_rows(values_only=True)
                header = tuple(next(source_rows))
                column = header.index("source_key")
                row_by_key = {
                    str(row[column]): index
                    for index, row in enumerate(source_rows, 2)
                    if any(value is not None for value in row)
                }
            finally:
                workbook.close()
            resolver, sheet = self.education_resolver, "teaching"
        elif declared.mapping_version == "f7_selected_form_v1":
            decoded = json.loads(raw)
            if (
                type(decoded) is not dict
                or set(decoded) != {"input_version", "family", "request"}
                or decoded["input_version"] != "f7_selected_form_v1"
                or decoded["family"] not in self.form_resolvers
                or declared.domain_options is not None
            ):
                raise FullScopeInputRejected("SOURCE_FORMAT_INVALID")
            model, resolver = self.form_resolvers[decoded["family"]]
            payload = model.model_validate_json(json.dumps(decoded["request"]))
            if payload.effective_at is None:
                raise FullScopeInputRejected("SOURCE_DOMAIN_OPTIONS_REQUIRED")
            sheet = "selected_form"
        else:
            from app.composition.school_fact_residence_product import ResidenceBatchInput

            if self.residence_resolver is None or declared.domain_options is not None:
                raise FullScopeInputRejected("SOURCE_DOMAIN_OPTIONS_INVALID")
            decoded = json.loads(raw)
            if (
                type(decoded) is not dict
                or set(decoded) != {"input_version", "request"}
                or decoded["input_version"] != "f7_residence_import_v1"
            ):
                raise FullScopeInputRejected("SOURCE_FORMAT_INVALID")
            payload = ResidenceBatchInput.model_validate_json(json.dumps(decoded["request"]))
            if payload.import_rows is None or payload.effective_at is None:
                raise FullScopeInputRejected("SOURCE_DOMAIN_IMPORT_REQUIRED")
            resolver, sheet = self.residence_resolver, "residence_request"
        if (
            declared.mapping_version != "f7_selected_form_v1"
            and getattr(payload, "import_mode", "incremental") != "incremental"
        ):
            raise FullScopeInputRejected("SOURCE_REPLACEMENT_MUST_USE_PACKAGE_SCOPE")
        # The request UUID identifies deterministic preparation only, never a
        # write/idempotency authorization. Formal execution uses the shared Writer.
        resolved = resolver(
            actor=actor,
            payload=payload,
            request_id=UUID(declared.source_sha256[:32]),
            effective_at=payload.effective_at,
        )
        if resolved.change_set.base_version != base_version:
            raise FullScopeInputRejected("SOURCE_DOMAIN_SNAPSHOT_CHANGED")
        return self._part(
            actor,
            declared,
            raw,
            resolved,
            sheet,
            row_by_key,
            fallback=(
                self._form_proof(resolver, payload, declared.source_sha256, resolved)
                if declared.mapping_version == "f7_selected_form_v1"
                else None
            ),
        )
