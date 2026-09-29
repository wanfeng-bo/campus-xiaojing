from app.composition.school_fact_family_staff_gateway import FamilyStaffToolGateway
from app.platform.assistant.tool_loop import ModelToolCall
from tests.test_f3_family_staff_gateway import context, gateway


def test_reused_confirmation_mechanism_keeps_the_new_fact_owner():
    owner = gateway()
    owner.module_ref = 'school_facts.education'
    owner.maintenance_fact_kinds = frozenset({'education_class'})
    result = owner.execute(ModelToolCall(call_id='1', name='school_facts.education.prepare', arguments={
        'fact_kind': 'education_class', 'target_ref': 'f4:education_class:object',
        'fields': {'display_name': '明确班名'}, 'reason': '正式更名',
    }), context())
    assert result.module_result.validation_receipt.safe_result_family == 'school_facts.education'
    assert result.model_content['tool_context']['action_owner'] == 'school_facts.education'
    assert result.fact_card['family_owner'] == 'school_facts.education'
    assert FamilyStaffToolGateway.module_ref == 'school_facts.family_staff'
