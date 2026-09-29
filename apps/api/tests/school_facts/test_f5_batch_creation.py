"""Same-batch creations retain their exact authorized creation identity in preview."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.composition.school_fact_education_product import EducationBatchInput
from tests.school_facts.test_f4_education_maintenance import environment, record

AT = datetime(2026, 9, 14, tzinfo=UTC)
NEW = {
    "teaching_offering": "offer:new",
    "offering_member_source": "source:new",
    "teaching_assignment": "teacher:new",
}


def setup():
    coordinator, actor = environment(
        [record("teaching_course", "course:math", display_name="数学")]
    )
    repo = coordinator.repository
    old_reference = repo.reference_for
    old_read = repo.read_records
    calls = []

    def reference_for(*args, **kw):
        if kw["source_key"] in NEW.values() and not kw.get("creation", False):
            raise PermissionError("unpublished object has no read permission")
        if kw.get("creation"):
            return f"f4create:{kw['fact_kind']}:{kw['source_key']}"
        return old_reference(*args, **kw)

    def read(*args, **kw):
        calls.append(kw)
        if any(k in NEW.values() for k in kw.get("source_keys") or ()) and not kw.get("creation"):
            raise PermissionError("unpublished root cannot be read as existing")
        return old_read(*args, **kw)

    repo.reference_for = reference_for
    repo.read_records = read
    return coordinator, actor, calls


def payload():
    offer = "f4create:teaching_offering:offer:new"
    changes = [
        dict(
            fact_kind="teaching_offering",
            creation_ref=offer,
            fields={"course_ref": "f4:teaching_course:course:math", "offering_status": "active"},
        ),
        dict(
            fact_kind="offering_member_source",
            creation_ref="f4create:offering_member_source:source:new",
            fields={
                "offering_ref": offer,
                "source_kind": "administrative_class",
                "class_ref": "f4:education_class:class:old",
            },
        ),
        dict(
            fact_kind="teaching_assignment",
            creation_ref="f4create:teaching_assignment:teacher:new",
            fields={
                "offering_ref": offer,
                "assignment_ref": "f3:assignment:staff:li",
                "teaching_role": "lead",
            },
        ),
    ]
    changes = [
        dict(**c, reason="明确本批三个新对象", operation="upsert", effective_at=AT.isoformat())
        for c in changes
    ]
    return EducationBatchInput.model_validate_json(
        json.dumps({**changes[0], "additional_changes": changes[1:]})
    )


def test_same_batch_three_creations_preview_without_existing_read_fallback():
    coordinator, actor, calls = setup()
    result = coordinator.resolve_change(
        actor=actor, payload=payload(), request_id=uuid4(), effective_at=AT
    )
    assert len(result.change_set.changes) == 3
    afters = [
        item["after"]
        for item in result.summary.impacts
        if item["fact_kind"] == "offering_member_source"
    ]
    assert afters[0]["offering_ref"] == "f4create:teaching_offering:offer:new"
    assert not any(
        kw.get("source_keys") == ("offer:new",) and not kw.get("creation") for kw in calls
    )


def test_reference_to_creation_absent_from_this_batch_never_becomes_public_creation():
    coordinator, actor, _ = setup()
    request = payload().additional_changes[0]
    standalone = EducationBatchInput.model_validate_json(request.model_dump_json())
    with pytest.raises((PermissionError, ValueError)):
        coordinator.resolve_change(
            actor=actor, payload=standalone, request_id=uuid4(), effective_at=AT
        )


def test_missing_creation_permission_is_rejected_before_any_preview():
    coordinator, actor, _ = setup()
    old = coordinator.repository.source_for_reference

    def source(**kw):
        if kw.get("creation") and kw["fact_kind"] == "teaching_assignment":
            raise PermissionError("creation revoked")
        return old(**kw)

    coordinator.repository.source_for_reference = source
    with pytest.raises(PermissionError):
        coordinator.resolve_change(
            actor=actor, payload=payload(), request_id=uuid4(), effective_at=AT
        )
