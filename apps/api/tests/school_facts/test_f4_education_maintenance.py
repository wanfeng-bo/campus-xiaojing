from datetime import UTC, datetime
from importlib import import_module

import pytest

AT = datetime(2026, 9, 1, tzinfo=UTC)


def segment(start, end=None, **extras):
    return {"effective_at": start, "valid_to": end, "content": {"display_name": "Class"}, **extras}


def snapshot(segments, **extra):
    module = import_module("app.composition.school_fact_education_maintenance")
    return module.snapshot_at(
        {"source_key": "class:1", "version": 3, "segments": segments, **extra},
        kind="education_class",
        at=AT,
    )


def test_ended_prefix_does_not_replace_current_segment():
    value = snapshot(
        [segment("2026-01-01T00:00:00Z", "2026-08-01T00:00:00Z"), segment("2026-08-01T00:00:00Z")]
    )
    assert value.effective_at.month == 8


def test_future_independent_segment_cannot_be_silently_discarded():
    value = snapshot(
        [segment("2026-01-01T00:00:00Z", "2026-10-01T00:00:00Z"), segment("2026-10-01T00:00:00Z")]
    )
    assert len(value.segments) == 2
    assert value.segments[1].effective_at.month == 10


def test_multiple_future_segments_cannot_choose_latest():
    value = snapshot(
        [segment("2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z"), segment("2026-11-01T00:00:00Z")]
    )
    assert len(value.segments) == 2
    assert value.effective_at.month == 10


def test_physical_cutoff_overrides_payload_and_prior_restoration_is_exact():
    current = segment(
        "2026-01-01T00:00:00Z", "2026-10-01T00:00:00Z", cause_source_key="new:1", cause_version=1
    )
    prior = segment("2026-01-01T00:00:00Z")
    value = snapshot([current], prior_segments=[prior])
    assert value.content["valid_to"] == datetime(2026, 10, 1, tzinfo=UTC)
    assert value.provenance.prior_content["valid_to"] is None


def test_explicit_absent_causal_proof_never_restores_an_independent_cutoff():
    current = segment(
        "2026-01-01T00:00:00Z",
        "2026-10-01T00:00:00Z",
        cause_source_key="future",
        cause_version=1,
        causal_prior=None,
    )
    value = snapshot([current], prior_segments=[segment("2026-01-01T00:00:00Z")])
    assert value.provenance is None


def environment(records, school_version=3):
    from types import SimpleNamespace

    from app.composition.school_fact_education_maintenance import EducationMaintenanceCoordinator
    from app.composition.school_fact_education_product import EducationProductAdapter
    from tests.school_facts.test_f4_education_reader import actor

    class Repository:
        def source_for_reference(self, *, reference, **kwargs):
            return reference.split(":", 2)[2]

        def reference_for(self, *, fact_kind, source_key, **kwargs):
            return f"f4:{fact_kind}:{source_key}"

        def read_records(self, actor, kind, source_keys=None, filter=None, **kwargs):
            selected = [
                r
                for r in records
                if r["fact_kind"] == kind
                and (source_keys is None or r["source_key"] in source_keys)
            ]
            if filter:
                selected = [
                    r
                    for r in selected
                    if any(
                        all(s["content"].get(k) == v for k, v in filter.items())
                        for s in r["segments"]
                    )
                ]
            if kwargs.get("creation"):
                selected = [{"source_key": source_keys[0], "version": 0, "status": "not_recorded"}]
            return SimpleNamespace(
                records=selected,
                school_fact_version=school_version,
                proof=SimpleNamespace(revalidate=lambda **kw: None, school_fact_version=3),
            )

    repo = Repository()
    return EducationMaintenanceCoordinator(
        EducationProductAdapter(repository=repo, external=repo), family=None
    ), actor()


def record(kind, key, start="2026-01-01T00:00:00Z", end=None, **content):
    return {
        "fact_kind": kind,
        "source_key": key,
        "version": 1,
        "segments": [dict(segment(start, end), content=content)],
        "status": "available",
    }


def test_bridge_transfer_returns_complete_endings_with_physical_time_and_cause():
    import json
    from uuid import uuid4

    from app.composition.school_fact_education_product import EducationBatchInput

    old = record(
        "student_class_membership",
        "old",
        student_source_key="student",
        class_source_key="old-class",
        school_relationship_source_key="relationship",
        membership_status="assigned",
    )
    role = record(
        "student_class_position",
        "role",
        student_source_key="student",
        class_source_key="old-class",
        position_source_key="monitor",
    )
    coordinator, actor = environment([old, role])
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            {
                "fact_kind": "student_class_membership",
                "creation_ref": "f4create:student_class_membership:new",
                "fields": {
                    "student_ref": "f4:student:student",
                    "class_ref": "f4:education_class:new-class",
                    "school_relationship_ref": "f4:student_school_relationship:relationship",
                    "membership_status": "assigned",
                },
                "reason": "Approved transfer",
                "purpose": "transfer",
            }
        )
    )
    result = coordinator.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=AT
    )
    changes = {c.source_key: c for c in result.change_set.changes}
    assert set(changes) == {"old", "role", "new"}
    assert changes["old"].effective_at == AT
    assert changes["role"].cause_source_key == "new"
    assert len(result.summary.impacts) == 3
    regroup = result.summary.regroup
    assert regroup["scope"] == "batch_only"
    assert regroup["periods"][0]["student_count"] == 1
    assert regroup["periods"][0]["departures"] == [
        {
            "student_ref": "f4:student:student",
            "from_class_ref": "f4:education_class:old-class",
            "to_class_ref": "f4:education_class:new-class",
            "status": "assigned",
        }
    ]


def test_bridge_cancel_restores_only_original_causal_cutoff():
    import json
    from uuid import uuid4

    from app.composition.school_fact_education_product import EducationBatchInput

    future = record(
        "student_class_membership",
        "future",
        start="2026-10-01T00:00:00Z",
        student_source_key="student",
        class_source_key="new-class",
        school_relationship_source_key="relationship",
        membership_status="assigned",
    )
    old = record(
        "student_class_membership",
        "old",
        end="2026-10-01T00:00:00Z",
        student_source_key="student",
        class_source_key="old-class",
        school_relationship_source_key="relationship",
        membership_status="assigned",
    )
    old["version"] = 2
    old["prior_segments"] = [{**old["segments"][0], "valid_to": None, "revision": 1}]
    old["segments"][0].update(cause_source_key="future", cause_version=1)
    coordinator, actor = environment([future, old])
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            {
                "fact_kind": "student_class_membership",
                "target_ref": "f4:student_class_membership:future",
                "operation": "cancel",
                "fields": {},
                "reason": "Cancel published plan",
            }
        )
    )
    result = coordinator.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=AT
    )
    changes = {c.source_key: c for c in result.change_set.changes}
    assert set(changes) == {"old", "future"}
    assert changes["old"].operation == "correct"
    assert changes["old"].content is None
    assert changes["old"].replacement_segments[0].valid_to is None
    assert changes["future"].effective_at == datetime(2026, 10, 1, tzinfo=UTC)


@pytest.mark.parametrize("full_access", [True, False])
def test_ssr_bridge_preserves_complete_original_content_and_period(full_access):
    import json
    from types import SimpleNamespace
    from uuid import uuid4

    from app.composition.school_fact_education_product import EducationChangeInput
    from app.platform.school_facts.f3_policy import F3_FACT_FIELDS

    coordinator, actor = environment(
        [
            record(
                "student_class_membership",
                "membership",
                student_source_key="student",
                class_source_key="class",
                school_relationship_source_key="relation",
                membership_status="assigned",
            )
        ]
    )
    original = {
        "student_source_key": "student",
        "student_number": "S001",
        "enrollment_status": "enrolled",
        "residence_mode": "day",
        "primary_campus_source_key": "campus",
        "administrative_campus_source_key": "campus",
    }
    end = datetime(2027, 8, 1, tzinfo=UTC)
    fact = SimpleNamespace(
        content=original,
        valid_to=end,
        school_fact_version=3,
        expected_version=1,
        label="School relationship",
    )
    permit = SimpleNamespace(
        scope=SimpleNamespace(
            field_names=F3_FACT_FIELDS["student_school_relationship"]
            if full_access
            else {"enrollment_status"}
        )
    )

    class Family:
        def source_for_reference(self, *args):
            return "student_school_relationship", "relation"

        def _read(self, **kwargs):
            assert kwargs["action"] == "read"
            return fact, None, permit

        def read_education_timeline(self, **kwargs):
            if not full_access:
                raise ValueError("需要全部原字段读取权限")
            return {
                "source_key": "relation",
                "version": 1,
                "school_fact_version": 3,
                "segments": [
                    {
                        "effective_at": "2026-01-01T00:00:00Z",
                        "valid_to": end.isoformat(),
                        "content": original,
                    }
                ],
            }

        def resolve_field_references(self, **kwargs):
            return kwargs["fields"]

        def public_field_references(self, **kwargs):
            return kwargs["fields"]

    coordinator.family = SimpleNamespace(repository=Family())
    item = EducationChangeInput.model_validate_json(
        json.dumps(
            {
                "fact_kind": "student_school_relationship",
                "target_ref": "family:relation",
                "operation": "correct",
                "fields": {"enrollment_status": "graduated"},
                "reason": "Historical graduation",
            }
        )
    )
    if not full_access:
        with pytest.raises(ValueError, match="原字段"):
            coordinator._school_relation(actor, item, uuid4(), AT)
    else:
        change = coordinator._school_relation(actor, item, uuid4(), AT).change_set.changes[0]
        assert change.content.student_number == "S001"
        assert change.content.valid_to == end
        assert change.content.enrollment_status == "graduated"
        from app.composition.school_fact_education_product import EducationBatchInput

        batch = EducationBatchInput.model_validate_json(item.model_dump_json())
        result = coordinator.resolve_change(
            actor=actor, payload=batch, request_id=uuid4(), effective_at=AT
        )
        assert {c.source_key for c in result.change_set.changes} == {"relation", "membership"}
        relation_impact = next(
            i for i in result.summary.impacts if i["fact_kind"] == "student_school_relationship"
        )
        assert relation_impact["before_segments"][0]["fields"]["enrollment_status"] == "enrolled"
        assert relation_impact["after_segments"][-1]["fields"]["enrollment_status"] == "graduated"


def test_ssr_terminal_guard_uses_explicit_period_permission():
    from types import SimpleNamespace

    from app.composition.school_fact_family_authority import StaffFamilyFactChangeSetGuard
    from app.platform.school_facts.f3_policy import F3_FACT_FIELDS

    assert "valid_to" in F3_FACT_FIELDS["student_school_relationship"]
    value = StaffFamilyFactChangeSetGuard._target(
        SimpleNamespace(
            content=None,
            fact_kind="student_school_relationship",
            source_key="relationship",
            expected_version=1,
            operation="end",
        )
    )
    assert value.field_names == ("valid_to",)


def test_ssr_timeline_requires_all_replaced_fields_not_only_cutoff():
    from types import SimpleNamespace

    from app.composition.school_fact_family_authority import StaffFamilyFactChangeSetGuard

    change = SimpleNamespace(
        content=None,
        fact_kind="student_school_relationship",
        source_key="relationship",
        expected_version=1,
        operation="correct",
        replacement_segments=(
            SimpleNamespace(
                content=SimpleNamespace(
                    model_fields_set={"student_source_key", "enrollment_status"}
                )
            ),
        ),
    )
    assert set(StaffFamilyFactChangeSetGuard._target(change).field_names) == {
        "student_source_key",
        "enrollment_status",
        "valid_to",
    }


@pytest.mark.parametrize("has_cutoff", [True, False])
def test_ssr_timeline_authorizes_complete_read_before_sql_and_rechecks_after(has_cutoff):
    from types import SimpleNamespace

    from app.composition.school_fact_family_staff_reader import FamilyStaffRepository
    from app.platform.school_facts.f3_policy import F3_FACT_FIELDS
    from tests.school_facts.test_f4_education_reader import actor

    fields = set(F3_FACT_FIELDS["student_school_relationship"])
    allowed = fields if has_cutoff else fields - {"valid_to"}
    events = []
    permit = SimpleNamespace(scope=SimpleNamespace(field_names=allowed), target=object())

    def authorize(*args):
        assert args[1] == "read"
        events.append("authorize")
        return permit

    class Projection:
        def education_timeline(self, **kw):
            kw["before_read"]()
            assert set(kw["field_names"]()) == fields
            events.append("sql")
            kw["after_read"]()
            return {
                "fact_kind": "student_school_relationship",
                "source_key": "relation",
                "version": 1,
                "school_fact_version": 3,
                "segments": [],
            }

    repository = FamilyStaffRepository(
        projection=Projection(),
        configuration=SimpleNamespace(),
        reference_key=b"test-private-key-only",
        authorize_scope=authorize,
        revalidate=lambda *args: events.append("revalidate"),
        clock=lambda: AT,
    )
    if has_cutoff:
        repository.read_education_timeline(actor=actor(), source_key="relation")
        assert events == ["authorize", "sql", "revalidate"]
    else:
        with pytest.raises(ValueError, match="原字段"):
            repository.read_education_timeline(actor=actor(), source_key="relation")
        assert events == ["authorize"]


def test_explicit_record_correction_preserves_writer_timeline_without_automatic_expansion():
    import json
    from uuid import uuid4

    from app.composition.school_fact_education_product import EducationBatchInput

    root = record("education_class", "class-root", display_name="Class")
    root["segments"][0]["valid_to"] = "2026-10-01T00:00:00Z"
    root["segments"].append(segment("2026-10-01T00:00:00Z"))
    coordinator, actor = environment([root])
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            {
                "fact_kind": "education_class",
                "target_ref": "f4:education_class:class-root",
                "operation": "correct",
                "fields": {"display_name": "Corrected class"},
                "reason": "Correct displayed name",
            }
        )
    )
    result = coordinator.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=AT
    )
    assert len(result.change_set.changes) == 1
    assert result.change_set.changes[0].content.display_name == "Corrected class"
