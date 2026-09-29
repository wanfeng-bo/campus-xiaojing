import json

import pytest
from pydantic import ValidationError

from app.composition.school_fact_directory_product import FactChangeInput, OwnReceiptTarget


def test_education_correction_and_original_receipt_keep_their_operation():
    payload = FactChangeInput.model_validate_json(json.dumps({
        'fact_kind': 'class_teacher_assignment', 'target_ref': 'f4:teacher:approved-ref',
        'operation': 'correct', 'fields': {}, 'reason': '更正学校已登记的交接日期',
    }))
    assert payload.operation == 'correct'
    receipt = OwnReceiptTarget(fact_kind='class_teacher_assignment', label='班主任安排',
                               reference='f4:teacher:approved-ref', operation='correct', target_version=2)
    assert receipt.operation == 'correct'


def test_education_cancel_cannot_silently_expand_legacy_contact_operations():
    with pytest.raises(ValidationError):
        FactChangeInput.model_validate_json(json.dumps({
            'fact_kind': 'official_contact', 'target_ref': 'f2:contact:approved-ref',
            'operation': 'cancel', 'fields': {}, 'reason': '不能复用新教育撤销语义',
        }))
