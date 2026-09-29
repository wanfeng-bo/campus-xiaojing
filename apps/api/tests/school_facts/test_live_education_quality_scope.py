"""School quality coverage never substitutes for target and field authorization."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.composition.school_fact_education_reader import EducationReadStale
from app.platform.school_facts.f2_policy import F2FactScope
from tests.school_facts.test_f4_education_quality import actor, fixture


def school_fixture():
    bridge, guard, authority, projection = fixture()
    who = actor()
    scope = F2FactScope(who.school_id, "fact_quality", (), coverage="school")
    guard.require_current = lambda *a, **kw: SimpleNamespace(
        scope_plan=SimpleNamespace(reference=scope.reference)
    )
    keys = tuple(f"quality:{i:03}" for i in range(205))
    state = SimpleNamespace(pages=0, batches=[], boundary=None)

    def scope_keys(**kwargs):
        state.pages += 1
        selected = tuple(k for k in keys if kwargs["after_key"] is None or k > kwargs["after_key"])
        more = len(selected) > kwargs["limit"]
        selected = selected[: kwargs["limit"]]
        return dict(
            keys=list(selected),
            has_more=more,
            next_after=selected[-1] if more else None,
            school_fact_version=projection.version,
        )

    def read(*, before_read, after_read, **kwargs):
        before_read()
        selected = kwargs["source_keys"]()
        assert len(selected) <= 100
        state.batches.append(selected)
        result = dict(
            school_fact_version=projection.version,
            next_valid_boundary=state.boundary,
            records=[
                dict(
                    source_key=k,
                    content=dict(
                        education_target_kind="education_class",
                        education_target_source_key="class:a",
                        target_fact_kind="class_teacher_assignment",
                        target_source_key="teacher:a",
                        field_name="teacher_role",
                    ),
                )
                for k in selected
            ],
        )
        after_read(result)
        return result

    projection.scope_keys, projection.read = scope_keys, read
    return bridge, guard, authority, projection, state


def read(bridge):
    return bridge.read_dependency(
        actor(),
        kind="fact_quality",
        target_kind="class_teacher_assignment",
        target_source_keys=("teacher:a",),
        field_names=("teacher_role",),
        owner_kind="education_class",
        owner_source_keys=("class:a",),
    )


def test_school_quality_reads_all_key_pages_through_original_target_field_projection():
    bridge, guard, _, _, state = school_fixture()
    result = read(bridge)
    assert len(result["records"]) == 205 and state.pages == 3
    assert max(map(len, state.batches)) <= 100
    guard.allowed = False
    with pytest.raises(PermissionError):
        result["proof"].revalidate()


def test_school_quality_does_not_enumerate_keys_before_target_fields_are_allowed():
    bridge, _, authority, _, state = school_fixture()
    authority.field_allowed = False
    with pytest.raises(PermissionError):
        read(bridge)
    assert state.pages == 0 and state.batches == []


def test_school_quality_rejects_version_change_between_metadata_pages():
    bridge, _, _, projection, state = school_fixture()
    original = projection.scope_keys

    def changed(**selection):
        if state.pages:
            projection.version += 1
        return original(**selection)

    projection.scope_keys = changed
    with pytest.raises(EducationReadStale):
        read(bridge)
    assert state.pages == 2


def test_school_quality_proof_expires_at_earliest_batch_boundary():
    bridge, _, _, _, state = school_fixture()
    now = [bridge.clock()]
    bridge.clock = lambda: now[0]
    deadline = now[0] + timedelta(minutes=1)
    state.boundary = deadline.isoformat()
    result = read(bridge)
    assert result["next_valid_boundary"] == deadline.isoformat()
    now[0] = deadline
    with pytest.raises(EducationReadStale):
        result["proof"].revalidate()


def test_school_quality_creation_still_requires_current_target_maintenance_fields():
    bridge, _, authority, _, _ = school_fixture()
    content = {
        "education_target_kind": "education_class",
        "education_target_source_key": "class:a",
        "target_fact_kind": "class_teacher_assignment",
        "target_source_key": "teacher:a",
        "field_name": "teacher_role",
        "owner_kind": "education",
    }
    change = SimpleNamespace(
        fact_kind="fact_quality",
        source_key="live:quality:new",
        expected_version=0,
        operation="upsert",
        content=SimpleNamespace(model_dump=lambda **_: content),
    )
    plan = SimpleNamespace(changes=(change,))
    proof = bridge.require_change_set(actor(), plan)
    authority.field_allowed = False
    with pytest.raises(PermissionError):
        proof.revalidate()
    with pytest.raises(PermissionError):
        bridge.require_change_set(actor(), plan)
    authority.field_allowed = True
    content["target_source_key"] = "teacher:other"
    with pytest.raises(PermissionError):
        bridge.require_change_set(actor(), plan)


def test_school_quality_signed_reference_roundtrip_does_not_enumerate_all_keys():
    from app.composition.school_fact_education_quality import EducationDependencyReferenceAdapter
    from app.platform.school_facts.school_scope_reference import (
        decode_school_scope_reference,
        encode_school_scope_reference,
    )

    bridge, guard, _, _, state = school_fixture()

    def reference_for(*, actor, fact_kind, source_key, creation):
        return encode_school_scope_reference(
            secret=b"test-secret-123456",
            family="f2",
            school_id=actor.school_id,
            assignment_id=actor.assignment_id,
            fact_kind=fact_kind,
            source_key=source_key,
            creation=creation,
        )

    def source_for(*, actor, fact_kind, reference, creation):
        return decode_school_scope_reference(
            reference,
            secret=b"test-secret-123456",
            family="f2",
            school_id=actor.school_id,
            assignment_id=actor.assignment_id,
            fact_kind=fact_kind,
            creation=creation,
        )

    adapter = EducationDependencyReferenceAdapter(
        bridge=bridge,
        education_repository=None,
        directory_repository=SimpleNamespace(reference_for=reference_for, source_for=source_for),
    )
    key = "汉" * 255
    ref = adapter.reference_for(actor(), "fact_quality", key)
    assert adapter.source_for(actor(), "fact_quality", ref) == key
    assert state.pages == 0
    with pytest.raises((ValueError, PermissionError)):
        adapter.source_for(actor(), "fact_quality", ref, creation=True)
    guard.allowed = False
    with pytest.raises(PermissionError):
        adapter.source_for(actor(), "fact_quality", ref)
