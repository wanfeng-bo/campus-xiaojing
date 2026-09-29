"""Production schema30 configuration/boot gates, isolated from databases and models."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from tests.assistant.test_f4_product_configuration import f4_document
from tests.school_facts.test_sf5b_writer_executor import _writer_settings


@pytest.mark.parametrize("revision", [29, 30, 31])
def test_product_accepts_exact_f4_runtime_reader_contract(tmp_path, revision):
    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration

    data = f4_document(tmp_path)
    data["runtime"]["postgres_schema_contract"] = data["reader"]["schema_contract"] = (
        f"school-facts-f4-v{revision}"
    )
    result = SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert result.education is not None and result.family is not None
    data["reader"]["schema_contract"] = f"school-facts-f4-v{30 if revision == 29 else 29}"
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("revision", [29, 30, 31])
def test_writer_kernel_requires_both_authorizers_and_constructs_with_them(revision):
    from app.composition.school_fact_writer_executor import (
        SqlAlchemySchoolFactWriterExecutorKernel as Kernel,
    )

    settings = _writer_settings().model_copy(
        update={"schema_contract": f"school-facts-f4-v{revision}"}
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with pytest.raises(ValueError, match="final authorization guard"):
            Kernel(engine=engine, settings=settings)
        with pytest.raises(ValueError, match="family actor"):
            Kernel(engine=engine, settings=settings, final_authorization_guard=MagicMock())
        value = Kernel(
            engine=engine,
            settings=settings,
            final_authorization_guard=MagicMock(),
            family_actor_validator=MagicMock(),
        )
        assert value._family_actor_validator is not None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "reader,writer,allowed",
    [
        (29, 29, True),
        (30, 30, True),
        (31, 31, True),
        (29, 30, False),
        (30, 29, False),
        (30, 31, False),
        (31, 30, False),
    ],
)
def test_writer_factory_validates_schema_pair_before_engine(
    monkeypatch, tmp_path, reader, writer, allowed
):
    import app.composition.school_fact_product_writer as m

    path = tmp_path / "writer.json"
    path.write_text("{}")
    monkeypatch.setenv("SCHOOL_FACT_F1_WRITER_CONFIG_FILE", str(path))

    def settings(revision, password):
        return SimpleNamespace(
            schema_contract=f"school-facts-f4-v{revision}",
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
    )
    monkeypatch.setattr(m.ProductWriterConfiguration, "model_validate_json", lambda _: config)

    class ReachedEngine(Exception):
        pass

    def stop(_):
        raise ReachedEngine()

    monkeypatch.setattr(m, "build_school_fact_reader_engine", stop)
    with pytest.raises(ReachedEngine if allowed else ValueError):
        m.create_school_fact_product_writer_app()


@pytest.mark.parametrize("revision", [30, 31])
def test_schema30_product_services_and_directory_pass_only_their_contract_gate(
    monkeypatch, revision
):
    import app.composition.school_fact_directory_services as directory
    import app.composition.school_fact_product_services as services

    class ReachedComposition(Exception):
        pass

    def stop(*a, **kw):
        raise ReachedComposition()

    settings = SimpleNamespace(schema_contract=f"school-facts-f4-v{revision}")
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


def test_f4_governance_private_contract_accepts31_but_not_unknown_future():
    from pydantic import TypeAdapter

    from app.composition.school_fact_f4_engineering_cli import EngineeringPrivateConfiguration

    adapter = TypeAdapter(
        EngineeringPrivateConfiguration.model_fields["expected_schema_contract"].annotation
    )
    assert adapter.validate_python("school-facts-f4-v30") == "school-facts-f4-v30"
    assert adapter.validate_python("school-facts-f4-v31") == "school-facts-f4-v31"
    with pytest.raises(ValidationError):
        adapter.validate_python("school-facts-f4-v32")
