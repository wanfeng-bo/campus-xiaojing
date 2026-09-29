"""Directory model declarations match the existing F2 owner boundary."""

from typing import Any

import pytest

from app.composition.school_fact_directory_gateway import (
    MODULE,
    DirectoryFactToolGateway,
    DirectoryModelPrepareInput,
)
from app.composition.school_fact_directory_product import FactChangeInput
from app.platform.school_facts.f2_policy import F2_FACT_KINDS


def _descriptor(action: str):
    return next(
        item for item in DirectoryFactToolGateway.descriptors if item.name == MODULE + action
    )


def _enum_values(schema: dict[str, Any], node: dict[str, Any]) -> set[str]:
    if "$ref" in node:
        return _enum_values(schema, schema["$defs"][node["$ref"].split("/")[-1]])
    return set(node.get("enum", ())).union(
        *(_enum_values(schema, item) for item in node.get("anyOf", ()))
    )


def test_directory_prepare_declares_only_kinds_accepted_by_its_owner():
    schema = _descriptor(".prepare").parameters

    assert _enum_values(schema, schema["properties"]["fact_kind"]) == F2_FACT_KINDS


def test_directory_query_describes_its_own_boundary_with_education_available():
    description = _descriptor(".query").description

    assert "当前未接入" not in description
    assert "本目录工具" in description
    assert "教育工具" in description
    assert "通用组织、任职或名称别名的查询结果不能替代这些教育关系" in description


def test_directory_prepare_keeps_existing_field_contracts_and_shared_http_kinds():
    schema = _descriptor(".prepare").parameters
    shared = FactChangeInput.model_json_schema()

    assert {name: value for name, value in schema["properties"].items() if name != "fact_kind"} == {
        name: value for name, value in shared["properties"].items() if name != "fact_kind"
    }
    assert schema["required"] == shared["required"]
    assert schema["additionalProperties"] is False
    for kind in ("student", "education_class", "teaching_assignment"):
        payload = FactChangeInput.model_validate(
            {
                "fact_kind": kind,
                "target_ref": f"fact:{kind}:reference",
                "fields": {},
                "reason": "明确校核依据",
            }
        )
        assert payload.fact_kind == kind
        with pytest.raises(ValueError):
            DirectoryModelPrepareInput.model_validate(payload.model_dump())


@pytest.mark.parametrize("kind", sorted(F2_FACT_KINDS))
def test_all_existing_f2_kinds_keep_the_shared_valid_input(kind):
    arguments = {
        "fact_kind": kind,
        "target_ref": f"f2:{kind}:reference",
        "fields": {},
        "reason": "明确校核依据",
    }

    assert DirectoryModelPrepareInput.model_validate(arguments).model_dump() == (
        FactChangeInput.model_validate(arguments).model_dump()
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"reason": 123},
        {"fields": {"source_key": "untrusted"}},
        {"creation_ref": "f2create:person:reference"},
        {"operation": "correct"},
        {"fields": {str(index): None for index in range(71)}},
    ],
)
def test_directory_execution_input_keeps_existing_rejections(changed):
    payload = {
        "fact_kind": "person",
        "target_ref": "f2:person:reference",
        "fields": {},
        "reason": "明确校核依据",
    }

    with pytest.raises(ValueError):
        FactChangeInput.model_validate({**payload, **changed})
    with pytest.raises(ValueError):
        DirectoryModelPrepareInput.model_validate({**payload, **changed})
