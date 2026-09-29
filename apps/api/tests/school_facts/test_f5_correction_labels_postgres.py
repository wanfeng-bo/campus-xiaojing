"""Owned-only correction labels keep the original selected-child target authority."""
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.school_facts.test_f5_family_teaching_postgres import owned_family  # noqa: F401
from tests.school_facts.test_f3_family_postgres import existing_fixture_writer
from tests.school_facts.test_f4_writer_postgres import apply

pytestmark=pytest.mark.skipif(not os.environ.get('F5_TEACHING_PG'),reason='explicit owned only')


def test_current_teaching_options_explain_course_and_teacher_without_internal_keys(owned_family):
    engines,guardian=owned_family
    with engines['writer'].connect() as connection:
        rows=connection.execute(text('SELECT * FROM app.school_fact_family_teaching_targets(:school_id,:adult,:relation,:version)'),guardian).mappings().all()
    selected=[r for r in rows if r['fact_kind']=='teaching_assignment' and r['field_name']=='assignment_source_key']
    assert selected
    assert all('任教' in r['label'] and ' · ' in r['label'] for r in selected)
    assert len({r['label'] for r in selected})==len(selected)
    assert all(r['fact_source_key'] not in r['label'] and 'source_key' not in r['label'] for r in rows)
    assert all(len(r['label'])<=800 for r in rows)


@pytest.mark.parametrize('role',['runtime','reader'])
def test_label_improvement_does_not_grant_other_roles_correction_target_access(owned_family,role):
    engines,guardian=owned_family
    with engines[role].connect() as connection, pytest.raises(DBAPIError):
        connection.execute(text('SELECT * FROM app.school_fact_family_teaching_targets(:school_id,:adult,:relation,:version)'),guardian)


def test_same_source_key_in_other_kind_cannot_supply_a_course_or_offering_name(owned_family):
    engines,guardian=owned_family
    sql=text('SELECT * FROM app.school_fact_family_teaching_targets(:school_id,:adult,:relation,:version)')
    with engines['writer'].connect() as connection:
        before=connection.execute(sql,guardian).mappings().all()
    target=sorted((r for r in before if r['fact_kind']=='teaching_assignment'),key=lambda r:r['fact_source_key'])[0]
    with engines['migration'].connect() as connection:
        stage=connection.scalar(text("SELECT source_key FROM school_education_facts WHERE school_id=:school AND kind='education_stage' ORDER BY source_key LIMIT 1"),{'school':guardian['school_id']})
        assert stage
        offering,course=connection.execute(text("""SELECT av.payload->>'offering_source_key',ov.payload->>'course_source_key'
        FROM school_education_facts a JOIN school_education_versions av ON av.fact_id=a.id AND av.superseded_at IS NULL
        JOIN school_education_facts o ON o.school_id=a.school_id AND o.kind='teaching_offering' AND o.source_key=av.payload->>'offering_source_key'
        JOIN school_education_versions ov ON ov.fact_id=o.id AND ov.superseded_at IS NULL
        WHERE a.school_id=:school AND a.kind='teaching_assignment' AND a.source_key=:key
        AND av.valid_from<=now() AND (av.valid_to IS NULL OR now()<av.valid_to)
        AND ov.valid_from<=now() AND (ov.valid_to IS NULL OR now()<ov.valid_to)"""),
            {'school':guardian['school_id'],'key':target['fact_source_key']}).one()
    actor,identity,context,service=existing_fixture_writer(engines)
    changes=[]
    for key in (course,offering):
        changes.extend([
            {'fact_kind':'teaching_subject','source_key':key,'operation':'upsert','expected_version':0,
             'effective_at':datetime.now(UTC),'content':{'display_name':'不可借作课程或开课名称','stage_source_keys':[stage]}},
            {'fact_kind':'course_subject','source_key':'f5-label-collision:'+uuid4().hex,'operation':'upsert','expected_version':0,
             'effective_at':datetime.now(UTC),'content':{'course_source_key':course,'subject_source_key':key}},
        ])
    apply((engines,actor,identity,context,service),changes)
    with engines['writer'].connect() as connection:
        after=connection.execute(sql,guardian).mappings().all()
    def selected(rows):
        return sorted((r['field_name'],r['label']) for r in rows if r['fact_source_key']==target['fact_source_key'] and r['fact_kind']=='teaching_assignment')
    assert selected(after)==selected(before)
    assert all('不可借作' not in label for _,label in selected(after))
