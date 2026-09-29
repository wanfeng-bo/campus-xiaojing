"""The additive live school scope must retain every formal execution guard."""

import json
from types import SimpleNamespace

import pytest

from app.composition.school_fact_live_settings import LiveSchoolFactProductConfiguration
from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from app.db.schema_contract import schema_contract_head
from tests.assistant.test_school_fact_live_settings import direct_only_live_document

CONTRACT = "school-facts-live-v57"


def test_live57_accepts_only_matching_reader_runtime_and_writer(tmp_path):
    document = direct_only_live_document(tmp_path)
    document["runtime"]["postgres_schema_contract"] = CONTRACT
    document["reader"]["schema_contract"] = CONTRACT
    config = LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(document))
    assert schema_contract_head(CONTRACT) == "20260928_57"
    assert config.assistant_enabled is False
    document["reader"]["schema_contract"] = "school-facts-f7-v56"
    with pytest.raises(ValueError):
        LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(document))


@pytest.mark.parametrize(
    "guards,error",
    [
        ({}, "final authorization"),
        ({"final_authorization_guard": lambda *_: None}, "family actor"),
        (
            {
                "final_authorization_guard": lambda *_: None,
                "family_actor_validator": lambda *_: None,
            },
            "package final",
        ),
    ],
)
def test_live57_writer_cannot_start_without_existing_final_guards(guards, error):
    with pytest.raises(ValueError, match=error):
        SqlAlchemySchoolFactWriterExecutorKernel(
            engine=None, settings=SimpleNamespace(schema_contract=CONTRACT), **guards
        )
