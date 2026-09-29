import json
from importlib import import_module

import pytest
from pydantic import ValidationError


def test_json_v3_policy_avoids_excel_cell_loss_and_requires_version():
    module = import_module("app.platform.master_data_imports.school_facts_json")
    text = "full policy\n" * 5000
    payload = {
        "input_version": "admin_v3",
        "base_version": 1,
        "changes": [
            {
                "operation": "upsert",
                "fact_kind": "policy_version",
                "source_key": "policy:long",
                "expected_version": 0,
                "effective_at": "2026-09-01T00:00:00Z",
                "content": {
                    "policy_source_key": "policy:root",
                    "business_code": "P",
                    "display_name": "Policy",
                    "category_code": "general",
                    "classification": "public",
                    "published_at": "2026-09-01T00:00:00Z",
                    "effective_from": "2026-09-01T00:00:00Z",
                    "content_text": text,
                },
            }
        ],
    }
    request = module.SchoolFactJsonInputV3.model_validate_json(json.dumps(payload))
    plan = module.SchoolFactJsonPlanBuilder().build(request)
    assert plan.schema_version == 2
    assert plan.changes[0].content.content_text == text
    for value in (None, "admin_v2"):
        with pytest.raises(ValidationError):
            module.SchoolFactJsonInputV3.model_validate_json(
                json.dumps({**payload, "input_version": value})
            )


def test_source_mapping_rejects_excel_overflow_before_openpyxl_truncates():
    from app.platform.master_data_imports.school_facts_excel import SCHOOL_FACT_SHEET_SPECS
    from app.platform.master_data_imports.source_mapping import (
        SchoolFactSourceAdapter,
        SourceMappingError,
    )

    rows = {name: [] for name in SCHOOL_FACT_SHEET_SPECS}
    name, spec = next(
        (name, spec)
        for name, spec in SCHOOL_FACT_SHEET_SPECS.items()
        if spec.fact_kind == "policy_version"
    )
    rows[name] = [{spec.headers[-1]: "X" * 32768}]
    with pytest.raises(SourceMappingError):
        SchoolFactSourceAdapter._standard_workbook(rows)
