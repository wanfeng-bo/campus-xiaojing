from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel


@pytest.mark.parametrize(
    "contract", ["school-facts-f2-v23", "school-facts-f2-v24", "school-facts-f2-v25"]
)
def test_f2_writer_cannot_be_constructed_without_final_authorization(contract):
    with pytest.raises(ValueError, match="final authorization guard"):
        SqlAlchemySchoolFactWriterExecutorKernel(
            engine=MagicMock(),
            settings=SimpleNamespace(schema_contract=contract),
        )


@pytest.mark.parametrize("enabled", [False, True])
def test_writer_uses_one_authority_for_every_input_version(monkeypatch, tmp_path, enabled):
    import app.composition.school_fact_product_writer as module

    path = tmp_path / "writer.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SCHOOL_FACT_F1_WRITER_CONFIG_FILE", str(path))
    reader, writer = MagicMock(), MagicMock()
    for setting in (reader, writer):
        setting.schema_contract = "school-facts-f2-v25" if enabled else "school-facts-f1-v22"
        setting.expected_school_id = setting.expected_anchor_id = "same"
        setting.expected_system_identifier = "same"
    configuration = SimpleNamespace(
        reader=reader,
        writer=writer,
        scope_id="scope",
        family=None,
        directory=object() if enabled else None,
    )
    monkeypatch.setattr(
        module.ProductWriterConfiguration, "model_validate_json", lambda _: configuration
    )
    for name in ("build_school_fact_reader_engine", "build_school_fact_writer_engine"):
        monkeypatch.setattr(module, name, lambda _: MagicMock())
    for name in ("verify_school_fact_reader_database", "verify_school_fact_writer_database"):
        monkeypatch.setattr(module, name, lambda *_a, **_kw: None)
    monkeypatch.setattr(module, "create_product_thin_service", lambda _: MagicMock())
    old_guard = MagicMock()
    new_guard = MagicMock(side_effect=PermissionError("exact target denied"))
    monkeypatch.setattr(module, "OfficialContactFinalAuthorizationGuard", lambda **_: old_guard)
    monkeypatch.setattr(
        module,
        "create_directory_read_components",
        lambda **_: SimpleNamespace(guard=new_guard, own_result_authorizer=MagicMock()),
    )
    kernel = MagicMock()
    monkeypatch.setattr(module, "SqlAlchemySchoolFactWriterExecutorKernel", kernel)
    monkeypatch.setattr(module, "CommandContextTransportCodec", lambda _: MagicMock())
    monkeypatch.setattr(module, "create_school_fact_writer_executor_app", lambda **_: MagicMock())
    module.create_school_fact_product_writer_app()
    final_guard = kernel.call_args.kwargs["final_authorization_guard"]
    for version in (1, 2):
        if enabled:
            with pytest.raises(PermissionError, match="exact target denied"):
                final_guard(MagicMock(), MagicMock(), SimpleNamespace(schema_version=version))
        else:
            final_guard(MagicMock(), MagicMock(), SimpleNamespace(schema_version=version))
    assert old_guard.call_count == (0 if enabled else 2)
    assert new_guard.call_count == (2 if enabled else 0)


def test_disabled_f3_writer_factory_keeps_f2_dispatch_and_rejects_family_before_db(
    monkeypatch, tmp_path
):
    import app.composition.school_fact_product_writer as module
    from app.composition.school_fact_family_settings import FamilyConfiguration
    from app.platform.school_facts.application import SchoolFactChangeRejected

    path = tmp_path / "writer.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SCHOOL_FACT_F1_WRITER_CONFIG_FILE", str(path))
    reader, writer = MagicMock(), MagicMock()
    for setting in (reader, writer):
        setting.schema_contract = "school-facts-f3-v27"
        setting.expected_school_id = setting.expected_anchor_id = "same"
        setting.expected_system_identifier = "same"
    family_config = FamilyConfiguration(
        enabled=False,
        approval_reference="rollback",
        scope_bindings=(),
        source_catalog={"student": ("student:f3",)},
    )
    configuration = SimpleNamespace(
        reader=reader,
        writer=writer,
        scope_id=__import__("uuid").uuid4(),
        directory=object(),
        family=family_config,
    )
    monkeypatch.setattr(
        module.ProductWriterConfiguration, "model_validate_json", lambda _: configuration
    )
    for name in ("build_school_fact_reader_engine", "build_school_fact_writer_engine"):
        monkeypatch.setattr(module, name, lambda _: MagicMock())
    for name in ("verify_school_fact_reader_database", "verify_school_fact_writer_database"):
        monkeypatch.setattr(module, name, lambda *_a, **_kw: None)
    monkeypatch.setattr(module, "create_product_thin_service", lambda _: MagicMock())
    directory_guard = MagicMock()
    directory_proof = MagicMock()
    directory_proof.allowed_targets = frozenset({("person", "person:f2")})
    directory_proof.revalidate.return_value = None
    monkeypatch.setattr(
        module,
        "create_directory_read_components",
        lambda **_: SimpleNamespace(
            guard=directory_guard, own_result_authorizer=lambda _context: directory_proof
        ),
    )
    family_proof = MagicMock()
    family_proof.allowed_targets = frozenset({("student", "student:f3")})
    family_proof.revalidate.return_value = None
    family_components = SimpleNamespace(
        change_set_guard=MagicMock(),
        thin=MagicMock(),
        actor_validator=MagicMock(),
    )
    monkeypatch.setattr(module, "create_family_authority_components", lambda **_: family_components)
    monkeypatch.setattr(module, "F3OwnResultAuthorizer", lambda **_: lambda _context: family_proof)
    kernel = MagicMock()
    monkeypatch.setattr(module, "SqlAlchemySchoolFactWriterExecutorKernel", kernel)
    monkeypatch.setattr(module, "CommandContextTransportCodec", lambda _: MagicMock())
    monkeypatch.setattr(module, "create_school_fact_writer_executor_app", lambda **_: MagicMock())
    module.create_school_fact_product_writer_app()
    final_guard = kernel.call_args.kwargs["final_authorization_guard"]
    session = MagicMock()
    family_change_set = MagicMock()
    family_change_set.changes = (SimpleNamespace(fact_kind="student", source_key="student:f3"),)
    family_change_set.model_copy.side_effect = lambda *, update: SimpleNamespace(
        changes=update["changes"]
    )
    with pytest.raises(SchoolFactChangeRejected):
        final_guard(session, MagicMock(), family_change_set)
    session.scalar.assert_not_called()
    directory_change_set = MagicMock()
    directory_change_set.changes = (SimpleNamespace(fact_kind="person", source_key="person:f2"),)
    directory_change_set.model_copy.side_effect = lambda *, update: SimpleNamespace(
        changes=update["changes"]
    )
    final_guard(session, MagicMock(), directory_change_set)
    directory_guard.assert_called_once()
    proof = kernel.call_args.kwargs["own_result_authorizer"](MagicMock())
    assert proof.allowed_targets == frozenset({("person", "person:f2"), ("student", "student:f3")})
