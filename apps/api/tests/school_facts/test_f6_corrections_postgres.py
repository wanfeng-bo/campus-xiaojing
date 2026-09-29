"""Real F6 selected-child report targets and immutable receipt difference checks."""

import json
from uuid import uuid4

import psycopg
import pytest

from tests.school_facts.test_f6_reader_postgres import reader_fixture  # noqa: F401


def writer(c):
    c.execute("RESET SESSION AUTHORIZATION")
    c.execute("SET SESSION AUTHORIZATION campus_ai_school_fact_writer")


def owner(c):
    c.execute("RESET SESSION AUTHORIZATION")
    c.execute("SET SESSION AUTHORIZATION campus_ai_migration")
    c.execute("SET ROLE campus_ai_db_owner")


def test_real_report_targets_are_selected_child_and_writer_only(reader_fixture):
    c, data = reader_fixture
    sql = "SELECT * FROM app.school_fact_family_residence_targets(%s,%s,%s,1)"
    args = (data['school'], data['adult'], data['relation'])
    with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
        c.execute(sql, args)
    writer(c)
    rows = c.execute(sql, args).fetchall()
    assert rows and {r[5] for r in rows} == {data['student']}
    assert {'student_bed_residence', 'dorm_bed', 'dorm_room', 'dormitory_building_profile'} == {r[2] for r in rows}
    assert all(r[0] not in {'occupant_count', 'roommates', 'student_name'} for r in rows)
    with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification), c.transaction():
        c.execute(sql, (data['school'], uuid4(), data['relation']))
    owner(c)
    c.execute("UPDATE guardian_relationships SET relationship_kind='recognized_caregiver',access_purpose='caregiver_student_summary' WHERE id=%s", (data['relation'],))
    writer(c)
    assert c.execute(sql, args).fetchall() == []


@pytest.mark.parametrize('change_name', [False, True])
def test_real_receipt_field_proof_rejects_noop_and_nonmatching_fields(reader_fixture, change_name):
    c, data = reader_fixture
    owner(c)
    c.execute("SET CONSTRAINTS ALL DEFERRED")
    key = data['keys']['dorm_bed']
    row = c.execute("SELECT v.id,v.display_name FROM dorm_bed_versions v JOIN dorm_beds b ON b.school_id=v.school_id AND b.id=v.bed_id WHERE b.school_id=%s AND b.source_key=%s", (data['school'], key)).fetchone()
    c.execute("UPDATE dorm_bed_versions SET superseded_at=statement_timestamp() WHERE id=%s", (row[0],))
    delta = {'id': str(uuid4()), 'version': 2, 'superseded_at': None,
             'display_name': '正式更正床名' if change_name else row[1]}
    c.execute("INSERT INTO dorm_bed_versions SELECT (jsonb_populate_record(NULL::dorm_bed_versions,to_jsonb(v)||%s::jsonb)).* FROM dorm_bed_versions v WHERE id=%s", (json.dumps(delta), row[0]))
    unit, lineage = uuid4(), uuid4()
    old = c.execute("SELECT unit_id FROM school_fact_change_lineage WHERE school_id=%s AND fact_kind='dorm_bed' AND fact_source_key=%s AND target_version=1", (data['school'], key)).fetchone()[0]
    old_batch=c.execute('SELECT batch_id FROM master_data_import_units WHERE id=%s',(old,)).fetchone()[0]
    batch, idem = uuid4(), uuid4()
    digest=batch.hex*2
    c.execute("INSERT INTO master_data_import_batches SELECT (jsonb_populate_record(NULL::master_data_import_batches,to_jsonb(b)||%s::jsonb)).* FROM master_data_import_batches b WHERE id=%s",(json.dumps({'id':str(batch),'request_id':str(uuid4()),'source_sha256':digest,'idempotency_key_hash':digest,'confirmation_token_sha256':digest,'planned_units':1,'succeeded_units':1}),old_batch))
    c.execute("INSERT INTO command_idempotency_records SELECT (jsonb_populate_record(NULL::command_idempotency_records,to_jsonb(i)||%s::jsonb)).* FROM command_idempotency_records i JOIN master_data_import_command_outcomes o ON o.idempotency_record_id=i.id WHERE o.batch_id=%s",(json.dumps({'id':str(idem),'idempotency_key_hash':digest,'resource_id':str(batch),'result_payload':{'resource_type':'master_data.import_batch','resource_id':str(batch),'resource_version':1,'outcome_code':'applied'}}),old_batch))
    c.execute("INSERT INTO master_data_import_command_outcomes SELECT (jsonb_populate_record(NULL::master_data_import_command_outcomes,to_jsonb(o)||%s::jsonb)).* FROM master_data_import_command_outcomes o WHERE batch_id=%s",(json.dumps({'id':str(uuid4()),'idempotency_record_id':str(idem),'batch_id':str(batch),'succeeded_units':[1],'executed_units':[1]}),old_batch))
    c.execute("INSERT INTO master_data_import_units SELECT (jsonb_populate_record(NULL::master_data_import_units,to_jsonb(u)||%s::jsonb)).* FROM master_data_import_units u WHERE id=%s", (json.dumps({'id':str(unit),'batch_id':str(batch),'sequence':1,'row_number':1,'target_version':2,'payload_sha256':unit.hex*2}),old))
    c.execute("INSERT INTO school_fact_change_lineage SELECT (jsonb_populate_record(NULL::school_fact_change_lineage,to_jsonb(l)||%s::jsonb)).* FROM school_fact_change_lineage l WHERE l.unit_id=%s", (json.dumps({'id':str(lineage),'batch_id':str(batch),'unit_id':str(unit),'target_version':2,'row_number':1,'source_sha256':digest}),old))
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")
    writer(c)
    sql = "SELECT app.school_fact_f6_field_changed(%s,'dorm_bed',%s,%s,%s)"
    assert c.execute(sql, (data['school'], key, 2, 'display_name')).fetchone()[0] is change_name
    assert c.execute(sql, (data['school'], key, 2, 'room_source_key')).fetchone()[0] is False
    assert c.execute(sql, (data['school'], key, 3, 'display_name')).fetchone()[0] is False
    assert c.execute(sql, (data['school'], key, 2, 'student_name')).fetchone()[0] is False
