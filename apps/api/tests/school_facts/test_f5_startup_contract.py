from types import SimpleNamespace

import pytest

from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from app.db.schema_contract import schema_contract_head


@pytest.mark.parametrize("version", [32, 33, 34, 35, 36, 37, 38, 39, 40, 41])
def test_f5_schema_head(version):
    assert schema_contract_head(f"school-facts-f5-v{version}") == f"20260914_{version}"


@pytest.mark.parametrize("version", [32, 33, 34, 35, 36, 37, 38, 39, 40, 41])
@pytest.mark.parametrize("guard", [None, lambda *_: None])
def test_new_writer_never_omits_final_or_family_guard(version, guard):
    settings = SimpleNamespace(schema_contract=f"school-facts-f5-v{version}")
    with pytest.raises(ValueError, match="final authorization|family actor"):
        SqlAlchemySchoolFactWriterExecutorKernel(
            engine=None, settings=settings, final_authorization_guard=guard
        )


def f5_document(tmp_path):
    from tests.assistant.test_f4_product_configuration import f4_document

    data = f4_document(tmp_path)
    data["purpose"] = "school-facts-f5-synthetic"
    data["runtime"]["postgres_schema_contract"] = "school-facts-f5-v41"
    data["reader"]["schema_contract"] = "school-facts-f5-v41"
    data["model"]["stage_budget"]["stage_id"] = "school-facts-f5"
    return data


def test_f5_complete_product_config_and_dependency_requirements(tmp_path):
    import copy
    import json

    from pydantic import ValidationError

    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration

    data = f5_document(tmp_path)
    assert SchoolFactProductConfiguration.model_validate_json(json.dumps(data)).education.enabled
    for path, value in [
        (("family",), None),
        (("education",), None),
        (("directory",), None),
        (("model", "stage_budget", "stage_id"), "school-facts-f4"),
        (("model", "stage_budget", "cap_microyuan"), 3000001),
    ]:
        invalid = copy.deepcopy(data)
        target = invalid
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValidationError):
            SchoolFactProductConfiguration.model_validate_json(json.dumps(invalid))


@pytest.mark.parametrize("version", [32, 33, 34, 35, 36, 37, 38, 39, 40])
def test_incomplete_f5_schema_cannot_enable_complete_product(tmp_path, version):
    import json

    from pydantic import ValidationError

    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration

    data = f5_document(tmp_path)
    data["runtime"]["postgres_schema_contract"] = f"school-facts-f5-v{version}"
    data["reader"]["schema_contract"] = f"school-facts-f5-v{version}"
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


def test_old_product_cannot_configure_new_teaching_coordinates(tmp_path):
    import json
    from uuid import UUID

    from pydantic import ValidationError

    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
    from tests.assistant.test_f4_product_configuration import f4_document

    data = f4_document(tmp_path)
    data["education"]["scope_bindings"] = [
        {
            "assignment_id": str(UUID(int=3)),
            "scope_id": str(UUID(int=4)),
            "operation_code": "school_facts.f5.teaching_subject.read",
        }
    ]
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


def test_writer_f5_coordinates_fail_before_old_schema_connection():
    from app.composition.school_fact_product_writer import require_f5_writer_schema

    settings = SimpleNamespace(schema_contract="school-facts-f4-v31")
    config = SimpleNamespace(
        reader=settings,
        writer=settings,
        education=SimpleNamespace(
            scope_bindings=(
                SimpleNamespace(operation_code="school_facts.f5.teaching_course.read"),
            ),
            creation_catalog={},
        ),
    )
    with pytest.raises(ValueError):
        require_f5_writer_schema(config)
    settings.schema_contract = "school-facts-f5-v41"
    require_f5_writer_schema(config)


@pytest.mark.parametrize("version", [35, 36, 37, 38, 39, 40, 41])
def test_disabled_f5_preserves_installed_schema_and_writer_guards(tmp_path, version):
    import json

    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
    from app.composition.school_fact_product_writer import require_f5_writer_schema

    data = f5_document(tmp_path)
    data["f5_features_enabled"] = False
    contract = f"school-facts-f5-v{version}"
    data["runtime"]["postgres_schema_contract"] = data["reader"]["schema_contract"] = contract
    config = SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert config.purpose == "school-facts-f5-synthetic"
    assert config.model.stage_budget.stage_id == "school-facts-f5"
    require_f5_writer_schema(
        SimpleNamespace(
            education=config.education,
            f5_features_enabled=False,
            reader=SimpleNamespace(schema_contract=contract),
            writer=SimpleNamespace(schema_contract=contract),
        )
    )


def test_schema41_product_and_directory_reach_composition_after_contract_gate(monkeypatch):
    import app.composition.school_fact_directory_services as directory
    import app.composition.school_fact_product_services as services

    class ReachedComposition(Exception):
        pass

    def stop(*args, **kwargs):
        raise ReachedComposition()

    settings = SimpleNamespace(schema_contract="school-facts-f5-v41")
    monkeypatch.setattr(services, "build_school_fact_reader_engine", stop)
    with pytest.raises(ReachedComposition):
        services.create_school_fact_product_services(
            reader_settings=settings, executor_settings=object(), scope_id=object()
        )
    monkeypatch.setattr(directory, "ConnectionBoundAuthority", stop)
    with pytest.raises(ReachedComposition):
        directory.create_directory_read_components(
            sessions=object(),
            settings=settings,
            configuration=object(),
            legacy_scope_id=object(),
            reference_key=b"synthetic",
        )


@pytest.mark.parametrize(
    "reader,writer,allowed", [(41, 41, True), (40, 40, False), (40, 41, False), (41, 40, False)]
)
def test_schema41_writer_factory_requires_matching_current_contract_before_connection(
    monkeypatch, tmp_path, reader, writer, allowed
):
    import app.composition.school_fact_product_writer as module

    path = tmp_path / "writer.json"
    path.write_text("{}")
    monkeypatch.setenv("SCHOOL_FACT_F1_WRITER_CONFIG_FILE", str(path))

    def settings(revision, password):
        return SimpleNamespace(
            schema_contract=f"school-facts-f5-v{revision}",
            expected_school_id="same",
            expected_anchor_id="same",
            expected_system_identifier="same",
            database_password=password,
        )

    config = SimpleNamespace(
        reader=settings(reader, "reader-secret"),
        writer=settings(writer, "writer-secret"),
        education=object(),
        family_leadership=None,
        family=object(),
        directory=object(),
        f5_features_enabled=True,
    )
    monkeypatch.setattr(module.ProductWriterConfiguration, "model_validate_json", lambda _: config)

    class ReachedConnection(Exception):
        pass

    def stop(_):
        raise ReachedConnection()

    monkeypatch.setattr(module, "build_school_fact_reader_engine", stop)
    with pytest.raises(ReachedConnection if allowed else ValueError):
        module.create_school_fact_product_writer_app()


def test_schema41_governance_private_contract_keeps40_and_rejects_unreviewed42():
    from pydantic import TypeAdapter, ValidationError

    from app.composition.school_fact_f5_engineering_cli import EngineeringPrivateConfiguration

    adapter = TypeAdapter(
        EngineeringPrivateConfiguration.model_fields["expected_schema_contract"].annotation
    )
    assert adapter.validate_python("school-facts-f5-v40") == "school-facts-f5-v40"
    assert adapter.validate_python("school-facts-f5-v41") == "school-facts-f5-v41"
    with pytest.raises(ValidationError):
        adapter.validate_python("school-facts-f5-v42")
