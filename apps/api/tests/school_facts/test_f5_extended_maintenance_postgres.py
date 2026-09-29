"""Additional finite maintenance evidence on the explicit owned fixture only."""
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.composition.school_fact_education_maintenance import snapshot_at
from app.domains.students.teaching_change_planner import plan_teaching_change, reschedule_teaching_future
from app.domains.students.teaching_excel import plan_teaching_import
from app.platform.school_facts.f5_policy import F5_FACT_FIELDS
from tests.school_facts.test_f5_temporal_linkage_postgres import (
    graph, owned_writer, change, read_rows, only_active, apply,
)

pytestmark = pytest.mark.skipif(not os.environ.get("F5_TEACHING_PG"), reason="owned only")


def snapshots(writer, coordinates, at):
    return tuple(snapshot_at(row, kind=kind, at=at)
                 for kind, keys in coordinates.items()
                 for row in read_rows(writer, kind, keys, at))


def planned(writer, changes, coordinates, now):
    rows = snapshots(writer, coordinates, now)
    result = plan_teaching_change(tuple(changes), rows, now=now,
        authorized_keys=frozenset(r.source_key for r in rows) | frozenset(c['source_key'] for c in changes),
        complete_snapshot=True)
    assert not result.issues, result.issues
    receipt = apply(writer, list(result.changes))
    print('F5_EXTENDED_COMMIT', receipt.batch_id, len(result.changes))
    return result


def test_source_A_B_A_preserves_independent_member_and_exact_periods(graph):
    writer, k, now, at, _ = graph
    key = k('source-a')
    t1, t2 = now + timedelta(days=1), now + timedelta(days=2)
    for version, stamp, fields in (
        (1, t1, {'source_kind':'independent_group','group_source_key':k('group')}),
        (2, t2, {'source_kind':'administrative_class','class_source_key':k('class-b')}),
    ):
        planned(writer, [change('offering_member_source', key, stamp,
            {'offering_source_key':k('offer-a'), **fields}, expected=version)],
            {'offering_member_source':[key]}, now)
    before = only_active(writer,'offering_member_source',[key],t1-timedelta(microseconds=1))[key]
    middle = only_active(writer,'offering_member_source',[key],t1)[key]
    after = only_active(writer,'offering_member_source',[key],t2)[key]
    assert before['class_source_key']==k('class-a') and 'group_source_key' not in before
    assert middle['group_source_key']==k('group') and 'class_source_key' not in middle
    assert after['class_source_key']==k('class-b') and 'group_source_key' not in after
    member = read_rows(writer,'teaching_group_member',[k('member')],t2)[0]
    assert member['version']==1


def test_shared_group_end_closes_both_offerings_in_single_formal_batch(graph):
    writer, k, now, at, _ = graph
    apply(writer,[change('offering_member_source',k('source-a'),now,
        {'offering_source_key':k('offer-a'),'source_kind':'independent_group','group_source_key':k('group')},expected=1)])
    coords = {'teaching_group':[k('group')], 'teaching_group_member':[k('member')],
        'teaching_offering':[k('offer-a'),k('offer-b')],
        'offering_member_source':[k('source-a'),k('source-b')],
        'teaching_assignment':[k('teacher-old')]}
    cut = now+timedelta(days=1)
    plan = planned(writer,[change('teaching_group',k('group'),cut,operation='end',expected=1)],coords,now)
    assert {c['source_key'] for c in plan.changes} == {v for values in coords.values() for v in values}
    for kind, keys in coords.items():
        assert only_active(writer,kind,keys,cut)=={}
        assert len(only_active(writer,kind,keys,cut-timedelta(microseconds=1)))==len(keys)


def test_one_offering_pause_resume_preserves_other_course_and_teacher_cutoff(graph):
    writer,k,now,at,_=graph
    for version,days,status in ((1,1,'suspended'),(2,5,'active')):
        planned(writer,[change('teaching_offering',k('offer-a'),now+timedelta(days=days),
            {'course_source_key':k('course'),'display_name':'隔离开课a','offering_status':status},expected=version)],
            {'teaching_offering':[k('offer-a'),k('offer-b')],
             'teaching_assignment':[k('teacher-old')],
             'offering_member_source':[k('source-a'),k('source-b')]},now)
    assert only_active(writer,'teaching_offering',[k('offer-a')],now+timedelta(days=2))[k('offer-a')]['offering_status']=='suspended'
    assert only_active(writer,'teaching_offering',[k('offer-a')],now+timedelta(days=5))[k('offer-a')]['offering_status']=='active'
    assert only_active(writer,'teaching_assignment',[k('teacher-old')],now+timedelta(days=5))=={}
    assert read_rows(writer,'teaching_offering',[k('offer-b')],now)[0]['version']==1
    assert read_rows(writer,'offering_member_source',[k('source-a')],now)[0]['version']==1


def test_future_reschedule_and_historical_correction_preserve_explicit_cutoff(graph):
    writer,k,now,at,_=graph
    key=k('future-course'); start=now+timedelta(days=2); end=now+timedelta(days=6)
    apply(writer,[change('teaching_course',key,start,{'display_name':'before','valid_to':end})])
    row=snapshots(writer,{'teaching_course':[key]},now)[0]
    moved=reschedule_teaching_future(change('teaching_course',key,now+timedelta(days=3),
        {'display_name':'before'},operation='correct',expected=1),row,from_at=start,now=now)
    apply(writer,[moved])
    assert only_active(writer,'teaching_course',[key],start)=={}
    assert only_active(writer,'teaching_course',[key],now+timedelta(days=3))[key]['display_name']=='before'
    assert only_active(writer,'teaching_course',[key],end)=={}
    # A distinct already-effective root proves recorded-time history is retained.
    historical=k('history-course')
    apply(writer,[change('teaching_course',historical,at,{'display_name':'incorrect'})])
    known_before = datetime.now(UTC)
    apply(writer,[change('teaching_course',historical,at,{'display_name':'corrected'},operation='correct',expected=1)])
    assert only_active(writer,'teaching_course',[historical],now)[historical]['display_name']=='corrected'
    assert read_rows(writer,'teaching_course',[historical],now)[0]['version']==2
    with writer[0]['reader'].connect() as conn:
        earlier = conn.scalar(text("SELECT app.school_fact_f5_targets(:school,'teaching_course',:keys,:fields,:at,:known,'{}'::jsonb)"),
            dict(school=writer[1].school_id, keys=[historical], fields=sorted(F5_FACT_FIELDS['teaching_course']),at=now,known=known_before))
    assert earlier['records'][0]['segments'][0]['content']['display_name']=='incorrect'


def test_incremental_omission_and_explicit_replacement_are_formal_and_scoped(graph):
    writer,k,now,at,_=graph
    keys=[k('import-one'),k('import-two')]
    apply(writer,[change('teaching_course',key,at,{'display_name':str(i)}) for i,key in enumerate(keys)])
    rows=snapshots(writer,{'teaching_course':keys},now)
    incremental=plan_teaching_import('teaching_course',[{'source_key':keys[0],'display_name':'updated'}],rows,effective_at=now)
    assert len(incremental.changes)==1
    apply(writer,list(incremental.changes))
    assert read_rows(writer,'teaching_course',[keys[1]],now)[0]['version']==1
    rows=snapshots(writer,{'teaching_course':keys},now)
    cut=now+timedelta(days=1)
    replacement=plan_teaching_import('teaching_course',[],rows,effective_at=cut,mode='replace',replacement_keys=frozenset({keys[0]}))
    assert len(replacement.changes)==1
    apply(writer,list(replacement.changes))
    assert set(only_active(writer,'teaching_course',keys,cut))=={keys[1]}


def test_explicit_shared_member_exit_and_new_join_leave_offerings_unchanged(graph):
    writer,k,now,at,_=graph
    apply(writer,[change('offering_member_source',k('source-a'),now,
        {'offering_source_key':k('offer-a'),'source_kind':'independent_group','group_source_key':k('group')},expected=1)])
    coords={'teaching_group':[k('group')], 'teaching_group_member':[k('member')],
        'teaching_offering':[k('offer-a'),k('offer-b')],
        'offering_member_source':[k('source-a'),k('source-b')]}
    cut=now+timedelta(days=1); rejoin=now+timedelta(days=2)
    result=planned(writer,[change('teaching_group_member',k('member'),cut,operation='end',expected=1)],coords,now)
    assert len(result.changes)==1
    assert {k('offer-a'),k('offer-b'),k('source-a'),k('source-b')} <= set(result.impact_keys)
    content={'group_source_key':k('group'),'student_source_key':k('student'),'school_relationship_source_key':k('ssr')}
    planned(writer,[change('teaching_group_member',k('explicit-rejoin'),rejoin,content)],coords,now)
    assert only_active(writer,'teaching_group_member',[k('member'),k('explicit-rejoin')],cut)=={}
    assert set(only_active(writer,'teaching_group_member',[k('member'),k('explicit-rejoin')],rejoin))=={k('explicit-rejoin')}
    for row in read_rows(writer,'teaching_offering',[k('offer-a'),k('offer-b')],rejoin):
        assert row['version']==1
