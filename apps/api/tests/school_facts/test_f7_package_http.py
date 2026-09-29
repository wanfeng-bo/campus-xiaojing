"""F7 private multi-source plan enters the same preflight and confirmation boundary."""

import base64
import json
from hashlib import sha256

import pytest

from app.platform.master_data_imports.school_facts_package import (
    FULL_SCOPE_JSON_MAPPING_SHA256,
)
from tests.school_facts.test_sf3_http import NOW, SCHOOL_UUID, _http_client


@pytest.mark.parametrize("version", ["school_facts_v2", "school_facts_v4"])
def test_package_reuses_registered_formal_workbook_without_changing_source_semantics(version):
    from io import BytesIO

    from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
    from app.platform.master_data_imports.school_facts_excel import (
        F6_SCHOOL_FACT_SHEET_SPECS,
        SCHOOL_FACT_SHEET_SPECS,
        SchoolFactWorkbookParser,
    )
    from tests.school_facts.test_sf3_http import _formal_workbook

    raw = _formal_workbook()
    specs = SCHOOL_FACT_SHEET_SPECS
    if version == "school_facts_v4":
        from openpyxl import load_workbook

        specs = F6_SCHOOL_FACT_SHEET_SPECS
        book = load_workbook(BytesIO(raw))
        book["00_导入说明"].cell(2, 1, version)
        for name, spec in specs.items():
            if name not in book.sheetnames:
                book.create_sheet(name).append(spec.headers)
        output = BytesIO()
        book.save(output)
        book.close()
        raw = output.getvalue()
    with _http_client(enable_package=True) as (client, _identities, writer):
        uploaded = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "synthetic.xlsx",
                "content_base64": base64.b64encode(raw).decode("ascii"),
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        source = ProtectedImportSourceRef(
            opaque_ref=uploaded.json()["source_ref"],
            source_sha256=sha256(raw).hexdigest(),
            filename_summary="synthetic.xlsx",
            size_bytes=len(raw),
            worksheet_names=tuple(specs),
        )
        parsed = SchoolFactWorkbookParser().parse(BytesIO(raw), source=source, base_version=1)
        assert parsed.template_version == version
        payload = _request(source.opaque_ref, raw)
        declared = payload["manifest"]["sources"][0]
        declared.update(
            mapping_version=parsed.mapping_version,
            mapping_config_sha256=parsed.mapping_config_sha256,
        )
        planned = client.post("/api/v1/school-facts/plans/package", json=payload)
        assert planned.status_code == 200, planned.text
        prepared = client.post(
            f"/api/v1/school-facts/plans/{planned.json()['plan_ref']}/confirmation",
            headers={"Idempotency-Key": "f7-workbook-package"},
        )
        assert prepared.status_code == 200, prepared.text
        lineage = writer.prepared[-1].provenance.row_lineage[0]
        assert lineage.source_slot == "school_source"
        assert lineage.source_sheet_code == parsed.row_lineage[0].source_sheet_code
        assert writer.prepared[-1].change_set.changes[0].source_key == "school:demo"


def test_package_workbook_cannot_claim_an_unregistered_mapping():
    from tests.school_facts.test_sf3_http import _formal_workbook

    raw = _formal_workbook()
    with _http_client(enable_package=True) as (client, _identities, _writer):
        uploaded = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "synthetic.xlsx",
                "content_base64": base64.b64encode(raw).decode("ascii"),
            },
        )
        payload = _request(uploaded.json()["source_ref"], raw)
        response = client.post("/api/v1/school-facts/plans/package", json=payload)
    assert response.status_code == 422


def _fact_source() -> bytes:
    return json.dumps(
        {
            "input_version": "school_facts_source_v5",
            "changes": [
                {
                    "operation": "upsert",
                    "fact_kind": "school",
                    "source_key": "school:demo",
                    "expected_version": 0,
                    "effective_at": NOW.isoformat(),
                    "content": {
                        "business_code": "XJ-001",
                        "display_name": "候选学校",
                        "timezone_name": "Asia/Shanghai",
                        "lifecycle_status": "active",
                    },
                }
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _request(source_ref: str, raw: bytes) -> dict:
    return {
        "manifest": {
            "input_version": "school_facts_v5",
            "school_id": str(SCHOOL_UUID),
            "base_version": 1,
            "sources": [
                {
                    "logical_source_code": "school_source",
                    "source_sha256": sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                    "mapping_version": "school_facts_source_v5",
                    "mapping_config_sha256": FULL_SCOPE_JSON_MAPPING_SHA256,
                    "responsibility_reference": "合成学校基础资料维护",
                    "fact_kinds": ["school"],
                }
            ],
        },
        "sources": [{"source_slot": "school_source", "source_ref": source_ref}],
    }


def test_package_preflight_and_confirmation_bind_distinct_provenance():
    raw = _fact_source()
    with _http_client(enable_package=True) as (client, _identities, writer):
        uploaded = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "synthetic-school-facts-v5.json",
                "content_base64": base64.b64encode(raw).decode("ascii"),
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        planned = client.post(
            "/api/v1/school-facts/plans/package",
            json=_request(uploaded.json()["source_ref"], raw),
        )
        assert planned.status_code == 200, planned.text
        assert planned.json()["is_clean"]
        prepared = client.post(
            f"/api/v1/school-facts/plans/{planned.json()['plan_ref']}/confirmation",
            headers={"Idempotency-Key": "f7-package-prepare"},
        )
        assert prepared.status_code == 200, prepared.text
    provenance = writer.prepared[-1].provenance
    assert provenance is not None
    assert provenance.channel == "package"
    assert provenance.source_manifest[0].source_sha256 == sha256(raw).hexdigest()
    assert provenance.row_lineage[0].source_slot == "school_source"


def test_package_refuses_wrong_school_before_reading_a_private_source():
    raw = _fact_source()
    with _http_client(enable_package=True) as (client, _identities, _writer):
        uploaded = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "synthetic-school-facts-v5.json",
                "content_base64": base64.b64encode(raw).decode("ascii"),
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        request = _request(uploaded.json()["source_ref"], raw)
        request["manifest"]["school_id"] = "11111111-1111-4111-8111-111111111111"
        planned = client.post("/api/v1/school-facts/plans/package", json=request)
    assert planned.status_code == 403


@pytest.mark.parametrize("nested", [False, True])
def test_raw_package_cannot_forge_domain_causal_provenance(nested):
    from app.platform.master_data_imports.school_facts_package import (
        FullScopeInputRejected,
        FullScopeSourceV5,
        parse_full_scope_fact_json_source,
    )
    from app.platform.school_facts.contracts import CanonicalFactChangeV1

    content = {"display_name": "Synthetic class"}
    row = {
        "fact_kind": "education_class",
        "source_key": "class:demo",
        "effective_at": NOW.isoformat(),
        "operation": "upsert",
        "expected_version": 0,
        "content": content,
    }
    forged = {"cause_source_key": "another:operation", "cause_version": 1}
    if nested:
        row.update(
            operation="correct",
            expected_version=1,
            content=None,
            correction_reason="correct recorded history",
            replacement_segments=[
                {
                    "effective_at": NOW.isoformat(),
                    "valid_to": None,
                    "content": content,
                    **forged,
                }
            ],
        )
    else:
        row.update(forged)
    # Such metadata is valid for trusted domain builders; uploaded bytes are not one.
    CanonicalFactChangeV1.model_validate_json(
        json.dumps(
            {
                **row,
                "schema_version": 4,
                "origin": {"sheet_code": "test", "row_number": 2},
            }
        )
    )
    raw = json.dumps({"input_version": "school_facts_source_v5", "changes": [row]}).encode()
    request = _request("test-source", raw)
    request["manifest"]["sources"][0]["fact_kinds"] = ["education_class"]
    source = FullScopeSourceV5.model_validate_json(json.dumps(request["manifest"]["sources"][0]))
    with pytest.raises(FullScopeInputRejected, match="DOMAIN_CAUSAL_PROVENANCE_REQUIRED"):
        parse_full_scope_fact_json_source(source, raw)
