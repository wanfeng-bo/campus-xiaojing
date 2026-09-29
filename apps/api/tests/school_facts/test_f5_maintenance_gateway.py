from types import SimpleNamespace

from app.composition.school_fact_education_gateway import EducationToolGateway
from app.platform.assistant.tool_loop import ModelToolCall
from tests.school_facts.test_f4_education_gateway import fixture


def test_teaching_maintenance_is_enabled_explicitly_without_expanding_old_gateway():
    _, old, context, _ = fixture()
    assert "teaching_assignment" not in old.maintenance_fact_kinds
    gateway = EducationToolGateway(resolve_actor=old.resolve_actor,query=old.education_query,product=SimpleNamespace(),describe_adapter=SimpleNamespace(describe=lambda **kw:{"fact_kind":kw["fact_kind"],"label":"任课安排","fields":[]}),teaching_enabled=True)
    assert "teaching_assignment" in gateway.maintenance_fact_kinds
    descriptor=next(d for d in gateway.descriptors if d.name.endswith('.describe'))
    assert "teaching_assignment" in descriptor.parameters['properties']['fact_kind']['enum']
    call=ModelToolCall(call_id='describe-teaching',name='school_facts.education.describe',arguments={'fact_kind':'teaching_assignment'})
    assert gateway.execute(call,context).fact_card['label']=='任课安排'
    assert old.execute(call,context).model_content['status']=='unsupported_selection'


def test_teacher_role_preview_keeps_machine_value_only_in_tool_context():
    from app.composition.school_fact_education_gateway import education_model_material
    view=education_model_material({'fact_kind':'teaching_assignment','fields':{'teaching_role':'co_teacher','assignment_ref':'opaque-assignment'}})
    assert view['fields']['teaching_role']=='共同任教'
    assert view['fields']['tool_context']['field_values']['teaching_role']=='co_teacher'
    assert view['fields']['tool_context']['assignment_ref']=='opaque-assignment'
