from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from app.platform.authorization.f2_reader_compiler import compile_f2
from app.platform.authorization.f2_reader_contracts import F2FactReadSelection
from app.platform.authorization.secure_reader_registry import f2_operation_registry
from app.platform.authorization.thin_contracts import ScopePlan
from app.platform.school_facts.f2_policy import F2FactScope
from tests.test_f2_secure_reader import NOW, SCHOOL, actor


def compile_fields(fields):
    scope = F2FactScope(SCHOOL, 'person', ('allowed.person',))
    return compile_f2(operation=f2_operation_registry().require('school_facts.f2.person.read'),
                      actor=actor(), scope_plan=ScopePlan(UUID(int=4), 'school_facts.f2', scope.reference, 1),
                      as_of=NOW, after_id=None, page_size=2,
                      selection=F2FactReadSelection(source_key='allowed.person', content_fields=fields))


def test_teacher_name_projection_does_not_fetch_other_person_content():
    relation = compile_fields(('display_name',))
    sql = str(relation.data_statement.selected_columns.content.compile(
        dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
    assert "'display_name'" in sql
    assert "'person_kind'" not in sql and "'business_code'" not in sql
    assert 'allowed.person' in str(relation.data_statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))


def test_related_projection_rejects_unregistered_fields_before_database():
    with pytest.raises(ValueError):
        compile_fields(('private_phone',))


def test_projection_fields_are_bound_to_the_read_cursor_and_old_default_is_unchanged():
    old = F2FactReadSelection(source_key='allowed.person')
    minimal = F2FactReadSelection(source_key='allowed.person', content_fields=('display_name',))
    assert old.binding_key == '["allowed.person",null,null,null,null]'
    assert minimal.binding_key != old.binding_key
