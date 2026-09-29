from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.composition.school_fact_education_execution import EducationWriterDispatch


def test_mixed_batch_checks_both_owners_and_does_not_fall_back_on_denial():
    education, retained = Mock(), Mock()
    changes = (SimpleNamespace(fact_kind="class_location"), SimpleNamespace(fact_kind="student_school_relationship"))
    batch = SimpleNamespace(changes=changes)
    batch.model_copy = lambda *, update: SimpleNamespace(**update)
    dispatch = EducationWriterDispatch(education=education, retained=retained)
    dispatch("session", "context", batch)
    assert education.call_args.args[2].changes == changes[:1]
    assert retained.call_args.args[2].changes == changes[1:]
    education.reset_mock()
    retained.reset_mock()
    education.side_effect = PermissionError("expired")
    with pytest.raises(PermissionError):
        dispatch("session", "context", batch)
    retained.assert_not_called()


def test_retained_fact_uses_retained_guard_even_if_education_enabled():
    education, retained = Mock(), Mock()
    batch = SimpleNamespace(changes=(SimpleNamespace(fact_kind="official_contact"),))
    batch.model_copy = lambda *, update: SimpleNamespace(**update)
    EducationWriterDispatch(education=education, retained=retained)("s", "c", batch)
    education.assert_not_called()
    retained.assert_called_once()


def test_explicit_education_dependency_contract_uses_its_scope_without_legacy_fallback():
    education, retained, dependency = Mock(), Mock(), Mock()
    change = SimpleNamespace(fact_kind='student_school_relationship', schema_version=3)
    batch = SimpleNamespace(changes=(change,))
    batch.model_copy = lambda *, update: SimpleNamespace(**update)
    dispatch = EducationWriterDispatch(education=education, retained=retained, dependency=dependency)
    dispatch('session', 'actor', batch)
    dependency.assert_called_once()
    retained.assert_not_called()
    dependency.side_effect = PermissionError('new scope expired')
    with pytest.raises(PermissionError):
        dispatch('session', 'actor', batch)
    retained.assert_not_called()
    change.schema_version = 2
    dispatch('session', 'actor', batch)
    retained.assert_called_once()
