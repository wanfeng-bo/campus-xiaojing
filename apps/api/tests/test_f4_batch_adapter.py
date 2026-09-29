from datetime import UTC, datetime
import json

import pytest

from app.composition.school_fact_education_product import EducationBatchInput


def test_batch_keeps_one_confirmation_and_rejects_raw_source_or_version():
    common = dict(fact_kind="class_location", target_ref="f4:class_location:opaque",
                  fields={"location_ref": "f2:location:opaque"}, reason="学校已确认调换教室")
    batch = EducationBatchInput.model_validate_json(json.dumps({**common, "additional_changes": [{
        **common, "target_ref": "f4:class_location:other",
    }]}))
    assert len(batch.additional_changes) == 1
    with pytest.raises(ValueError):
        EducationBatchInput(**{**common, "expected_version": 1})
    with pytest.raises(ValueError):
        EducationBatchInput(**{**common, "fields": {"source_key": "untrusted"}})


def test_old_school_relationship_history_correct_is_only_explicit_education_boundary():
    common = dict(fact_kind="student_school_relationship", target_ref="f3:student_school_relationship:opaque",
                  operation="correct", fields={"enrollment_status": "suspended"},
                  reason="学校补正已生效的休学记录", effective_at=datetime(2026, 9, 1, tzinfo=UTC))
    value = EducationBatchInput(**common)
    assert value.operation == "correct"


def test_batch_rejects_duplicate_targets_before_confirmation():
    common = dict(fact_kind="class_location", target_ref="f4:class_location:opaque",
                  fields={}, reason="学校调整")
    with pytest.raises(ValueError):
        EducationBatchInput(**common, additional_changes=[common])
