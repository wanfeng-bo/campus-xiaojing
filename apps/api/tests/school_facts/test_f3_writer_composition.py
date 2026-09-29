from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from tests.school_facts.test_sf5b_writer_executor import _context, _writer_settings


def test_f3_writer_requires_final_guard_and_current_domain_actor_validator():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    settings = _writer_settings().model_copy(update={"schema_contract": "school-facts-f3-v26"})
    try:
        with pytest.raises(ValueError):
            SqlAlchemySchoolFactWriterExecutorKernel(engine=engine, settings=settings)
        with pytest.raises(ValueError):
            SqlAlchemySchoolFactWriterExecutorKernel(
                engine=engine, settings=settings, final_authorization_guard=Mock()
            )
        validator = Mock()
        guard = Mock()
        kernel = SqlAlchemySchoolFactWriterExecutorKernel(
            engine=engine,
            settings=settings,
            final_authorization_guard=guard,
            family_actor_validator=validator,
        )
        service = kernel._service(context=_context(), session_binding="unit")
        assert service._family_actor_validator is validator
        assert service._writer._family_actor_validator is validator
        assert service._writer._final_authorization_guard is guard
    finally:
        engine.dispose()


def test_f3_final_boundary_uses_locked_current_epoch_and_never_falls_back():
    from types import SimpleNamespace

    from app.composition.school_fact_family_authority import FamilyFactAuthorityDenied
    from app.composition.school_fact_family_execution import FamilyFinalAuthorizationGuard
    from app.platform.school_facts.application import SchoolFactChangeRejected

    context = _context()
    cs = Mock()
    guard = Mock()
    session = Mock()
    session.scalar.return_value = 7
    permit = SimpleNamespace(
        item_permits=(
            SimpleNamespace(basis=SimpleNamespace(governance=SimpleNamespace(assignment_epoch=7))),
        )
    )
    guard.require_change_set.return_value = permit
    final = FamilyFinalAuthorizationGuard(guard)
    final(session, context, cs)
    guard.final_guard.assert_called_once()
    session.scalar.return_value = 8
    with pytest.raises(SchoolFactChangeRejected):
        final(session, context, cs)
    guard.require_change_set.side_effect = FamilyFactAuthorityDenied("denied")
    with pytest.raises(SchoolFactChangeRejected):
        final(session, context, cs)


def test_f3_disjoint_writer_ownership_keeps_existing_directory_and_no_retry():
    from types import SimpleNamespace

    from app.composition.school_fact_family_execution import FamilyWriterDispatch

    f3 = Mock()
    f2 = Mock()
    dispatch = FamilyWriterDispatch(
        family=f3,
        directory=f2,
        configuration=SimpleNamespace(
            source_catalog={"person": ("person:f3",)},
            creation_catalog={"assignment": {"assignment:f3": "本次任职"}},
        ),
    )
    assert dispatch.is_family("person", "person:f3")
    assert dispatch.is_family("assignment", "assignment:f3")
    assert dispatch.is_family("guardian_relationship", "unknown")
    assert not dispatch.is_family("person", "person:f2")
    cs = Mock()
    cs.changes = (SimpleNamespace(fact_kind="person", source_key="person:f3"),)
    f3.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        dispatch(Mock(), _context(), cs)
    f2.assert_not_called()


def test_f3_rollback_switch_rejects_new_family_writes_before_database_work():
    from app.composition.school_fact_family_execution import FamilyFinalAuthorizationGuard
    from app.platform.school_facts.application import SchoolFactChangeRejected

    dependency = Mock()
    session = Mock()
    guard = FamilyFinalAuthorizationGuard(dependency, enabled=False)
    with pytest.raises(SchoolFactChangeRejected, match="暂停"):
        guard(session, _context(), Mock())
    session.scalar.assert_not_called()
    dependency.require_change_set.assert_not_called()


def test_f3_rollout_switch_is_strict_and_does_not_remove_schema_or_scopes():
    from pydantic import ValidationError
    from app.composition.school_fact_family_settings import FamilyConfiguration

    config = FamilyConfiguration(approval_reference="approved-f3", scope_bindings=(), enabled=False)
    assert config.enabled is False
    assert config.approval_reference == "approved-f3"
    assert FamilyConfiguration(approval_reference="approved-f3", scope_bindings=()).enabled is True
    with pytest.raises(ValidationError):
        FamilyConfiguration(approval_reference="approved-f3", scope_bindings=(), enabled="false")
