"""F7 package input remains a candidate until current authorization and Writer confirmation."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from uuid import UUID

import pytest
from openpyxl import Workbook

from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from app.platform.master_data_imports.school_facts_excel import (
    F6_SCHOOL_FACT_SHEET_SPECS,
    SCHOOL_FACT_SHEET_SPECS,
    SchoolFactWorkbookParser,
)
from app.platform.master_data_imports.school_facts_package import (
    FULL_SCOPE_JSON_MAPPING_SHA256,
    FullScopeInputManifestV5,
    FullScopeInputRejected,
    FullScopePackageBuilder,
    FullScopeSourcePart,
    parse_full_scope_fact_json_source,
    require_full_scope_change_capacity,
    source_parts_from_workbook_plan,
)
from app.platform.school_facts.contracts import CanonicalFactChangeV1
from app.platform.school_facts.package_integrity import (
    require_full_scope_provenance_integrity,
)
from app.platform.school_facts.ports import (
    SchoolFactChangeProvenance,
    SchoolFactRowProvenance,
)

SCHOOL_ID = UUID("11111111-1111-4111-8111-111111111111")
AT = datetime(2026, 9, 24, tzinfo=UTC)


def manifest(*, sources=None, mode="incremental", scopes=()):
    return FullScopeInputManifestV5.model_validate_json(
        json.dumps(
            {
                "input_version": "school_facts_v5",
                "school_id": str(SCHOOL_ID),
                "base_version": 6,
                "mode": mode,
                "replacement_scopes": scopes,
                "sources": sources
                if sources is not None
                else [
                    {
                        "logical_source_code": "source_a",
                        "source_sha256": sha256(b"alpha").hexdigest(),
                        "size_bytes": 5,
                        "mapping_version": "f7_a",
                        "mapping_config_sha256": sha256(b"f7_a mapping").hexdigest(),
                        "responsibility_reference": "synthetic published input owner",
                        "fact_kinds": ["person"],
                    }
                ],
            },
            default=str,
        )
    )


def change(*, kind="person", key="person:a", version=1):
    return CanonicalFactChangeV1.model_validate(
        {
            "schema_version": version,
            "operation": "end",
            "fact_kind": kind,
            "source_key": key,
            "expected_version": 1,
            "effective_at": AT,
            "origin": {"sheet_code": "old_sheet", "row_number": 2},
            "content": None,
        }
    )


def part(slot="source_a", raw=b"alpha", changes=None):
    changes = (change(),) if changes is None else changes
    lineage = tuple(
        SchoolFactRowProvenance(
            fact_kind=item.fact_kind,
            fact_source_key=item.source_key,
            raw_source_sha256=sha256(raw).hexdigest(),
            source_slot=slot,
            source_sheet_code="old_sheet",
            source_row_number=index + 2,
            target_field_codes=("operation",),
            source_field_codes=("operation",),
        )
        for index, item in enumerate(changes)
    )
    return FullScopeSourcePart(
        logical_source_code=slot,
        original_bytes=raw,
        changes=changes,
        row_lineage=lineage,
    )


def test_package_source_and_canonical_plan_are_bound_for_writer():
    built = FullScopePackageBuilder().build(manifest=manifest(), parts=(part(),))
    provenance = SchoolFactChangeProvenance(
        channel="package",
        raw_source_sha256=built.raw_source_sha256,
        template_version=built.input_version,
        mapping_version="f7_v1",
        mapping_config_sha256=sha256(b"f7_v1").hexdigest(),
        source_size_bytes=sum(item.size_bytes for item in built.source_manifest),
        worksheet_names=(),
        source_manifest=built.source_manifest,
        row_lineage=built.row_lineage,
    )
    require_full_scope_provenance_integrity(provenance, built.change_set)
    with pytest.raises(ValueError, match="摘要"):
        require_full_scope_provenance_integrity(
            provenance,
            built.change_set.model_copy(update={"source_sha256": sha256(b"other").hexdigest()}),
        )


def test_versioned_fact_source_parser_supplies_server_lineage_not_client_coordinates():
    raw = json.dumps(
        {
            "input_version": "school_facts_source_v5",
            "changes": [
                {
                    "operation": "end",
                    "fact_kind": "person",
                    "source_key": "person:a",
                    "expected_version": 1,
                    "effective_at": AT.isoformat(),
                    "content": None,
                }
            ],
        }
    ).encode()
    declared = manifest(
        sources=[
            {
                **manifest().sources[0].model_dump(mode="json"),
                "source_sha256": sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "mapping_version": "school_facts_source_v5",
                "mapping_config_sha256": FULL_SCOPE_JSON_MAPPING_SHA256,
            }
        ]
    )
    parsed = parse_full_scope_fact_json_source(declared.sources[0], raw)
    assert parsed.changes[0].fact_kind == "person"
    assert parsed.row_lineage[0].source_slot == "source_a"
    assert parsed.row_lineage[0].source_row_number == 2
    assert parsed.changes[0].origin.sheet_code == "f7_source"
    with pytest.raises(FullScopeInputRejected):
        parse_full_scope_fact_json_source(
            declared.sources[0], raw.replace(b"person:a", b"person:b")
        )


def test_package_parser_keeps_structural_lineage_for_valid_empty_fact_content():
    raw = json.dumps({
        "input_version": "school_facts_source_v5",
        "changes": [{
            "operation": "upsert", "fact_kind": "policy", "source_key": "policy:empty",
            "expected_version": 0, "effective_at": AT.isoformat(), "content": {},
        }],
    }).encode()
    source = manifest(sources=[{
        **manifest().sources[0].model_dump(mode="json"),
        "source_sha256": sha256(raw).hexdigest(), "size_bytes": len(raw),
        "mapping_version": "school_facts_source_v5",
        "mapping_config_sha256": FULL_SCOPE_JSON_MAPPING_SHA256,
        "fact_kinds": ["policy"],
    }]).sources[0]
    parsed = parse_full_scope_fact_json_source(source, raw)
    assert parsed.changes[0].content is not None
    assert parsed.row_lineage[0].target_field_codes == ("operation",)
    assert parsed.row_lineage[0].source_field_codes == ("operation",)


def test_manifest_incremental_omission_defaults_to_unknown_and_requires_explicit_replacement():
    assert manifest().coverage_status == "unknown"
    with pytest.raises(ValueError):
        manifest(mode="replace")
    selected = manifest(
        mode="replace",
        scopes=[
            {
                "fact_kind": "person",
                "source_keys": ["person:a", "person:b"],
                "effective_at": AT,
            }
        ],
    )
    assert selected.replacement_scopes[0].source_keys == ("person:a", "person:b")
    with pytest.raises(ValueError):
        manifest(sources=[{**manifest().sources[0].model_dump(), "coverage_status": "complete"}])


def test_package_revalidates_mixed_old_schemas_as_one_schema_four_with_ordered_lineage():
    sources = [
        manifest().sources[0].model_dump(mode="json"),
        {
            "logical_source_code": "source_b",
            "source_sha256": sha256(b"beta").hexdigest(),
            "size_bytes": 4,
            "mapping_version": "f7_b",
            "mapping_config_sha256": sha256(b"f7_b mapping").hexdigest(),
            "responsibility_reference": "synthetic teaching owner",
            "fact_kinds": ["teaching_subject"],
        },
    ]
    selected = manifest(sources=sources)
    result = FullScopePackageBuilder().build(
        manifest=selected,
        parts=(
            part(changes=(change(),)),
            part(
                "source_b", b"beta", (change(kind="teaching_subject", key="subject:a", version=3),)
            ),
        ),
    )
    assert result.change_set.schema_version == 4
    assert {item.schema_version for item in result.change_set.changes} == {4}
    assert tuple(item.logical_source_code for item in result.source_manifest) == (
        "source_a",
        "source_b",
    )
    assert tuple(item.source_slot for item in result.row_lineage) == ("source_a", "source_b")
    assert result.change_set.changes[1].origin.sheet_code == "f7_s2"
    assert result.change_set.base_version == 6


def test_package_digest_binds_source_order_mapping_and_changes():
    first = manifest()
    built = FullScopePackageBuilder().build(manifest=first, parts=(part(),))
    changed_mapping = manifest(
        sources=[{**first.sources[0].model_dump(mode="json"), "mapping_version": "f7_b"}]
    )
    changed = FullScopePackageBuilder().build(manifest=changed_mapping, parts=(part(),))
    assert built.raw_source_sha256 != changed.raw_source_sha256
    assert built.change_set.source_sha256 != changed.change_set.source_sha256
    changed_config = manifest(
        sources=[
            {
                **first.sources[0].model_dump(mode="json"),
                "mapping_config_sha256": sha256(b"different registered mapping").hexdigest(),
            }
        ]
    )
    assert (
        FullScopePackageBuilder().build(manifest=changed_config, parts=(part(),)).raw_source_sha256
        != built.raw_source_sha256
    )


def test_package_refuses_mismatched_raw_bytes_lineage_and_undeclared_kind():
    builder = FullScopePackageBuilder()
    with pytest.raises(FullScopeInputRejected, match="SOURCE_CONTENT_MISMATCH"):
        builder.build(manifest=manifest(), parts=(part(raw=b"other"),))
    wrong_lineage = part()
    wrong_lineage = FullScopeSourcePart(
        logical_source_code="source_a",
        original_bytes=b"alpha",
        changes=wrong_lineage.changes,
        row_lineage=(
            SchoolFactRowProvenance(
                fact_kind="person",
                fact_source_key="person:someone_else",
                raw_source_sha256=sha256(b"alpha").hexdigest(),
                source_slot="source_a",
                source_sheet_code="old_sheet",
                source_row_number=2,
                target_field_codes=("operation",),
                source_field_codes=("operation",),
            ),
        ),
    )
    with pytest.raises(FullScopeInputRejected, match="SOURCE_LINEAGE_MISMATCH"):
        builder.build(manifest=manifest(), parts=(wrong_lineage,))
    with pytest.raises(FullScopeInputRejected, match="FACT_KIND_UNDECLARED"):
        builder.build(
            manifest=manifest(),
            parts=(part(changes=(change(kind="teaching_subject", key="subject:a", version=3),)),),
        )


def test_manifest_has_package_limits_independent_of_old_v2_v4_limits():
    old = manifest().sources[0].model_dump(mode="json")
    sources = [
        {
            **old,
            "logical_source_code": f"source_{index}",
            "source_sha256": sha256(str(index).encode()).hexdigest(),
            "size_bytes": 1,
        }
        for index in range(33)
    ]
    with pytest.raises(ValueError):
        manifest(sources=sources)
    with pytest.raises(ValueError):
        manifest(
            sources=[
                {**old, "size_bytes": 10 * 1024 * 1024},
                {**old, "logical_source_code": "source_b"},
            ]
        )
    require_full_scope_change_capacity(50_000)
    with pytest.raises(FullScopeInputRejected, match="CHANGE_COUNT_EXCEEDED"):
        require_full_scope_change_capacity(50_001)


@pytest.mark.parametrize("version", ["school_facts_v2", "school_facts_v4"])
def test_old_complete_workbook_is_only_a_validated_source_in_new_package(version):
    specs = F6_SCHOOL_FACT_SHEET_SPECS if version == "school_facts_v4" else SCHOOL_FACT_SHEET_SPECS
    book = Workbook()
    book.remove(book.active)
    for name, spec in specs.items():
        sheet = book.create_sheet(name)
        sheet.append(spec.headers)
        if name == "00_导入说明":
            sheet.append([version, "synthetic"])
        if name == "01_学校":
            row = {
                "操作": "end",
                "稳定标识": "school:one",
                "预期版本": 1,
                "生效时间": "2026-09-24T00:00:00Z",
            }
            sheet.append([row.get(header) for header in spec.headers])
    stream = BytesIO()
    book.save(stream)
    book.close()
    content = stream.getvalue()
    source = ProtectedImportSourceRef.model_validate_json(
        json.dumps(
            {
                "opaque_ref": "f7-legacy",
                "source_sha256": sha256(content).hexdigest(),
                "filename_summary": "school.xlsx",
                "size_bytes": len(content),
                "worksheet_names": list(specs),
            }
        )
    )
    old = SchoolFactWorkbookParser().parse(BytesIO(content), source=source, base_version=6)
    parts = source_parts_from_workbook_plan(old, {"formal_workbook": content})
    selected = manifest(
        sources=[
            {
                "logical_source_code": "formal_workbook",
                "source_sha256": source.source_sha256,
                "size_bytes": len(content),
                "mapping_version": version,
                "mapping_config_sha256": old.mapping_config_sha256,
                "responsibility_reference": "synthetic school owner",
                "fact_kinds": ["school"],
            }
        ]
    )
    new = FullScopePackageBuilder().build(manifest=selected, parts=parts)
    assert new.change_set.schema_version == 4
    assert new.change_set.changes[0].fact_kind == "school"
    assert new.row_lineage[0].source_sheet_code == old.row_lineage[0].source_sheet_code
    assert old.template_version == version


def test_long_policy_attachment_is_checked_byte_for_byte_and_not_truncated():
    content_text = " 正文\n" * 10_000
    policy = CanonicalFactChangeV1.model_validate(
        {
            "schema_version": 2,
            "operation": "upsert",
            "fact_kind": "policy_version",
            "source_key": "policy:one",
            "expected_version": 0,
            "effective_at": AT,
            "origin": {"sheet_code": "policy", "row_number": 2},
            "content": {
                "policy_source_key": "policy:root",
                "business_code": "P1",
                "display_name": "制度",
                "category_code": "general",
                "classification": "public",
                "published_at": AT,
                "effective_from": AT,
                "content_text": content_text,
            },
        }
    )
    attachment = content_text.encode("utf-8")
    sources = [
        {
            **manifest().sources[0].model_dump(mode="json"),
            "fact_kinds": ["policy_version"],
        },
        {
            "logical_source_code": "policy_text",
            "source_sha256": sha256(attachment).hexdigest(),
            "size_bytes": len(attachment),
            "mapping_version": "f7_text",
            "mapping_config_sha256": sha256(b"utf8-text").hexdigest(),
            "responsibility_reference": "synthetic policy publishing owner",
            "purpose": "policy_text",
            "attached_policy_source_key": "policy:one",
            "fact_kinds": [],
        },
    ]
    plan = FullScopePackageBuilder().build(
        manifest=manifest(sources=sources),
        parts=(part(changes=(policy,)), part("policy_text", attachment, ())),
    )
    assert plan.change_set.changes[0].content.content_text == content_text
    assert plan.attachment_bindings[0].source_slot == "policy_text"
    assert plan.attachment_bindings[0].source_sha256 == sha256(attachment).hexdigest()
    with pytest.raises(FullScopeInputRejected, match="POLICY_TEXT_MISMATCH"):
        FullScopePackageBuilder().build(
            manifest=manifest(
                sources=[
                    sources[0],
                    {
                        **sources[1],
                        "source_sha256": sha256(b"different").hexdigest(),
                        "size_bytes": 9,
                    },
                ]
            ),
            parts=(part(changes=(policy,)), part("policy_text", b"different", ())),
        )
