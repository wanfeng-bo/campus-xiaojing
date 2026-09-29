"""F7 multi-file provenance survives Product-to-Writer transport without impersonating Excel."""

from dataclasses import replace
from hashlib import sha256

import pytest

from app.platform.school_facts.executor_contracts import SchoolFactExecutorProvenance
from app.platform.school_facts.ports import (
    SchoolFactChangeProvenance,
    SchoolFactPolicyTextAttachment,
    SchoolFactRowProvenance,
    SchoolFactSourceArtifact,
)


def _digest(text: bytes) -> str:
    return sha256(text).hexdigest()


def _package() -> SchoolFactChangeProvenance:
    source = SchoolFactSourceArtifact("source_a", _digest(b"source"), 6)
    attachment = SchoolFactSourceArtifact("policy_a", _digest(b"policy"), 6)
    return SchoolFactChangeProvenance(
        channel="package",
        raw_source_sha256=_digest(b"manifest"),
        template_version="school_facts_v5",
        mapping_version="f7_v1",
        mapping_config_sha256=_digest(b"mapping"),
        source_size_bytes=12,
        worksheet_names=(),
        source_manifest=(source, attachment),
        row_lineage=(
            SchoolFactRowProvenance(
                fact_kind="person",
                fact_source_key="person:a",
                raw_source_sha256=source.source_sha256,
                source_slot="source_a",
                source_sheet_code="people",
                source_row_number=2,
                target_field_codes=("display_name",),
                source_field_codes=("name",),
            ),
        ),
        policy_attachments=(
            SchoolFactPolicyTextAttachment(
                source_slot="policy_a",
                source_sha256=attachment.source_sha256,
                fact_source_key="policy:a",
            ),
        ),
    )


def test_package_channel_and_attachment_survive_executor_round_trip():
    source = _package()
    transported = SchoolFactExecutorProvenance.from_domain(source)
    assert transported.channel == "package"
    assert transported.to_domain() == source


def test_package_rejects_missing_or_duplicate_attachment_source():
    source = _package()
    with pytest.raises(ValueError):
        replace(source, source_manifest=source.source_manifest[:1])


def test_legacy_admin_and_excel_cannot_smuggle_package_attachments():
    source = _package()
    for channel in ("admin", "excel"):
        with pytest.raises(ValueError):
            replace(source, channel=channel)
