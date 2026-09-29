import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.composition.school_fact_directory_product import (
    FactChangeInput,
    FactChangeSummary,
    ResolvedFactChange,
)
from app.composition.school_fact_residence_dependencies import ResidenceEligibilityAdapter
from app.composition.school_fact_residence_product import ResidenceBatchInput
from app.platform.school_facts.contracts import CanonicalFactChangeSetV3


def test_dependency_update_keeps_original_owner_and_expands_same_confirmed_batch():
    now = datetime(2026, 9, 22, tzinfo=UTC)
    cs = CanonicalFactChangeSetV3.model_validate_json(
        json.dumps(
            {
                "schema_version": 3,
                "base_version": 1,
                "source_sha256": "a" * 64,
                "changes": [
                    {
                        "schema_version": 3,
                        "fact_kind": "student_school_relationship",
                        "source_key": "student-relation",
                        "expected_version": 1,
                        "operation": "end",
                        "effective_at": now,
                        "content": None,
                        "origin": {"sheet_code": "original-owner", "row_number": 1},
                    }
                ],
            },
            default=str,
        )
    )
    checked = []
    summary = FactChangeSummary(
        fact_kind="student_school_relationship",
        label="在校资料",
        reference="f3:relation:original",
        operation="end",
        before=None,
        after=None,
        effective_at=now,
        reason="正式离校",
        source_label="正式来源",
    )
    result = ResolvedFactChange(cs, summary, lambda actor: checked.append(actor))
    seen = []

    def expand(**kwargs):
        seen.append(kwargs)
        return kwargs["resolved"]

    adapter = ResidenceEligibilityAdapter(
        education=SimpleNamespace(
            maintenance=SimpleNamespace(resolve_change=lambda **kw: result),
            family=SimpleNamespace(adapter=SimpleNamespace(resolve_change=lambda **kw: result)),
        ),
        lifecycle=SimpleNamespace(expand=expand),
    )
    payload = ResidenceBatchInput(
        fact_kind="student_school_relationship",
        target_ref="f3:relation:original",
        operation="end",
        reason="正式离校",
        fields={},
    )
    actual = adapter.resolve_change(
        actor="actor", payload=payload, request_id=uuid4(), effective_at=now
    )
    assert actual.change_set.schema_version == actual.change_set.changes[0].schema_version == 4
    assert actual.change_set.changes[0].fact_kind == "student_school_relationship"
    actual.summary_revalidate("actor")
    assert checked == ["actor"] and seen[0]["resolved"] is actual


def test_residence_dependency_cannot_create_student_or_supply_unreviewed_side_changes():
    with pytest.raises(ValueError):
        ResidenceBatchInput(
            fact_kind="student_school_relationship",
            creation_ref="f3create:relation:extra",
            operation="upsert",
            reason="create",
            fields={},
        )
    with pytest.raises(ValueError):
        ResidenceBatchInput(
            fact_kind="student_school_relationship",
            target_ref="f3:relation:original",
            operation="end",
            reason="end",
            fields={},
            additional_changes=(
                FactChangeInput(
                    fact_kind="student_bed_residence",
                    target_ref="f6:residence:extra",
                    operation="end",
                    reason="end",
                    fields={},
                ),
            ),
        )


class FixedFamilyQuery:
    def __init__(self, profile=None):
        self.calls = []
        self.profile = profile or {
            "status": "known",
            "tool_context": {"student_ref": "f3:student:chosen"},
        }

    def query(self, **kwargs):
        self.calls.append(kwargs)
        assert 1 <= kwargs.get("limit", 20) <= 20
        assert "query_text" not in kwargs
        assert bool(kwargs.get("related_ref")) == bool(kwargs.get("related_field"))
        return {"kind": "directory_facts", "status": "known", "records": [], "truncated": False}

    def query_eligibility(self, **kwargs):
        return {**self.query(**kwargs), "formal_future_owner": True}

    def student_profile(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["fields"] == ("name",)
        return self.profile

    def student_eligibility_profile(self, **kwargs):
        return self.student_profile(**kwargs)


def query_adapter(query):
    return ResidenceEligibilityAdapter(
        education=SimpleNamespace(family=SimpleNamespace(query=query)), lifecycle=None
    )


@pytest.mark.parametrize("limit", [None, 25, 100, 7])
def test_eligibility_query_respects_existing_f3_bound(limit):
    query = FixedFamilyQuery()
    query_adapter(query).query(actor="actor", limit=limit)
    assert query.calls[-1]["limit"] == (7 if limit == 7 else 20)


def test_returned_student_reference_is_paired_without_widening_the_filter():
    query = FixedFamilyQuery()
    query_adapter(query).query(actor="actor", related_ref="f3:student:chosen", limit=25)
    assert query.calls[-1]["related_field"] == "student_ref"
    assert query.calls[-1]["related_ref"] == "f3:student:chosen"


@pytest.mark.parametrize(
    "selection",
    [
        {"related_ref": "f2:location:campus"},
        {"related_field": "student_ref"},
        {"reference": "f3:relationship:one", "name": "学生"},
        {"query_text": "甲", "name": "乙"},
        {"cursor": "opaque"},
        {"limit": True},
    ],
)
def test_invalid_eligibility_selection_fails_before_any_read(selection):
    query = FixedFamilyQuery()
    with pytest.raises(ValueError):
        query_adapter(query).query(actor="actor", **selection)
    assert query.calls == []


@pytest.mark.parametrize("field", ["name", "query_text"])
def test_name_uses_existing_bounded_student_selection_then_exact_ssr(field):
    query = FixedFamilyQuery()
    query_adapter(query).query(actor="actor", **{field: "学生"})
    assert query.calls[0]["name"] == "学生"
    assert query.calls[1]["related_ref"] == "f3:student:chosen"
    assert query.calls[1]["related_field"] == "student_ref"


def test_number_only_eligibility_uses_the_same_f3_owner_exact_filter():
    query = FixedFamilyQuery()
    at = datetime(2026, 9, 22, tzinfo=UTC)
    result = query_adapter(query).query(actor="actor", student_number="F3-A001", as_of=at)
    assert result["formal_future_owner"] is True
    assert len(query.calls) == 1
    assert query.calls[0]["student_number"] == "F3-A001"
    assert query.calls[0]["as_of"] == at
    assert query.calls[0]["fact_kind"] == "student_school_relationship"
    assert "name" not in query.calls[0]


def test_name_and_number_eligibility_preserves_intersection_in_student_owner():
    query = FixedFamilyQuery()
    query_adapter(query).query(actor="actor", name="学生", student_number="F3-A001")
    assert query.calls[0]["name"] == "学生"
    assert query.calls[0]["student_number"] == "F3-A001"
    assert query.calls[1]["related_ref"] == "f3:student:chosen"
    assert "student_number" not in query.calls[1]


def test_f6_number_filter_contract_does_not_apply_to_residence_or_facilities():
    from app.composition.school_fact_residence_gateway import ResidenceQueryInput

    assert (
        ResidenceQueryInput(
            fact_kind="student_school_relationship", student_number="F3-A001"
        ).student_number
        == "F3-A001"
    )
    for kind in ("student_bed_residence", "dorm_bed", "dorm_room", "fact_quality"):
        with pytest.raises(ValueError):
            ResidenceQueryInput(fact_kind=kind, student_number="F3-A001")


@pytest.mark.parametrize("status", ["ambiguous", "incomplete", "object_unresolved"])
def test_uncertain_name_never_falls_back_to_an_ssr_catalog(status):
    profile = {
        "status": status,
        "candidates": [{"name": "学生", "tool_context": {"reference": "f3:student:one"}}],
    }
    query = FixedFamilyQuery(profile)
    result = query_adapter(query).query(actor="actor", name="学生")
    assert len(query.calls) == 1
    assert result["status"] == status and result["records"] == []
    assert result["candidates"] == profile["candidates"]


@pytest.mark.parametrize(
    "selection,expected",
    [
        ({"related_field": "student_ref", "related_ref": "f3:student:chosen"}, "known"),
        ({"related_ref": "f2:location:campus"}, "unsupported_selection"),
    ],
)
def test_gateway_ssr_filter_contract_is_bounded_and_errors_are_safe(selection, expected):
    from app.composition.school_fact_residence_gateway import ResidenceToolGateway
    from app.platform.assistant.tool_loop import ModelToolCall
    from tests.school_facts.test_f6_preflight_feedback import context, gateway

    original = gateway()
    query = FixedFamilyQuery()
    owner = ResidenceToolGateway(
        resolve_actor=original.resolve_actor,
        query=original.query,
        product=original.product,
        describe_adapter=None,
        eligibility_adapter=query_adapter(query),
    )
    result = owner.execute(
        ModelToolCall(
            call_id="query",
            name="school_facts.residence.query",
            arguments={"fact_kind": "student_school_relationship", **selection},
        ),
        context(),
    )
    assert result.model_content["status"] == expected


def test_future_ssr_history_rechecks_current_scope_and_keeps_requested_time():
    from app.composition.school_fact_residence_gateway import ResidenceToolGateway
    from app.platform.assistant.tool_loop import ModelToolCall
    from tests.school_facts.test_f6_preflight_feedback import context, gateway

    base = gateway()
    query = FixedFamilyQuery()
    current = {"allowed": True}
    original = query.query_eligibility

    def read(**selection):
        result = original(**selection)
        return result if current["allowed"] else {**result, "status": "forbidden"}

    query.query_eligibility = read
    owner = ResidenceToolGateway(
        resolve_actor=base.resolve_actor,
        query=base.query,
        product=base.product,
        describe_adapter=None,
        eligibility_adapter=query_adapter(query),
    )
    future = datetime.now(UTC) + timedelta(days=1)
    result = owner.execute(
        ModelToolCall(
            call_id="future",
            name="school_facts.residence.query",
            arguments={
                "fact_kind": "student_school_relationship",
                "student_number": "N001",
                "as_of": future.isoformat(),
            },
        ),
        context(),
    )
    assert result.history_check.valid()
    assert all(call["as_of"] == future for call in query.calls)
    current["allowed"] = False
    assert not result.history_check.valid()


def lifecycle_environment():
    from tests.school_facts.test_f4_education_maintenance import environment, record

    records = [
        record(
            "student_class_membership",
            "member",
            student_source_key="student",
            class_source_key="class",
            school_relationship_source_key="relation",
            membership_status="assigned",
        ),
        record(
            "student_class_position",
            "position",
            student_source_key="student",
            class_source_key="class",
            position_source_key="monitor",
        ),
        record(
            "teaching_group_member",
            "teaching",
            student_source_key="student",
            group_source_key="group",
            school_relationship_source_key="relation",
        ),
        record(
            "teaching_group_member",
            "other-relation-member",
            student_source_key="student",
            group_source_key="other-group",
            school_relationship_source_key="other-relation",
        ),
    ]
    relation = record(
        "student_school_relationship",
        "relation",
        student_source_key="student",
        student_number="one",
        enrollment_status="enrolled",
        residence_mode="boarding",
        primary_campus_source_key="campus",
        administrative_campus_source_key="campus",
    )
    relation["school_fact_version"] = 3
    coordinator, actor = environment(records)
    coordinator.canonical_version = 4
    calls = []

    def timeline(**kwargs):
        calls.append(kwargs)
        return relation

    coordinator.family = SimpleNamespace(
        repository=SimpleNamespace(
            source_for_reference=lambda *a: ("student_school_relationship", "relation"),
            read_education_timeline=timeline,
            resolve_field_references=lambda **kw: kw["fields"],
            public_field_references=lambda **kw: kw["fields"],
        )
    )
    return coordinator, actor, relation, records, calls


@pytest.mark.parametrize(
    "operation,fields",
    [
        ("end", {}),
        ("revoke", {}),
        ("correct", {"enrollment_status": "withdrawn"}),
    ],
)
def test_all_ssr_lifecycle_operations_expand_exact_f4_f5_before_residence(operation, fields):
    coordinator, actor, _, _, _ = lifecycle_environment()
    at = datetime(2026, 9, 1, tzinfo=UTC)
    seen = []

    def expand(**kwargs):
        seen.extend(c.source_key for c in kwargs["resolved"].change_set.changes)
        return kwargs["resolved"]

    adapter = ResidenceEligibilityAdapter(
        education=SimpleNamespace(maintenance=coordinator, family=coordinator.family),
        lifecycle=SimpleNamespace(expand=expand),
    )
    coordinator.family.adapter = SimpleNamespace(
        resolve_change=lambda **kw: pytest.fail("SSR bypassed its original education owner")
    )
    result = adapter.resolve_change(
        actor=actor,
        payload=ResidenceBatchInput(
            fact_kind="student_school_relationship",
            target_ref="f3:relationship:relation",
            operation=operation,
            fields=fields,
            reason="正式结束期间",
        ),
        request_id=uuid4(),
        effective_at=at,
    )
    assert set(seen) == {"relation", "member", "position", "teaching"}
    changes = {c.source_key: c for c in result.change_set.changes}
    for key in ("member", "position", "teaching"):
        assert changes[key].replacement_segments[-1].valid_to == at
    assert result.summary.operation == operation
    assert len(result.summary.impacts) == 4
    result.summary_revalidate(actor)


def test_ssr_terminal_clips_later_segments_and_preserves_other_continuous_membership():
    from app.composition.school_fact_education_product import EducationBatchInput
    from tests.school_facts.test_f4_education_maintenance import record

    coordinator, actor, relation, records, _ = lifecycle_environment()
    at = datetime(2026, 9, 1, tzinfo=UTC)
    relation["segments"][0]["valid_to"] = "2026-10-01T00:00:00Z"
    relation["segments"].append(
        {**relation["segments"][0], "effective_at": "2026-10-01T00:00:00Z", "valid_to": None}
    )
    records.append(
        record(
            "student_class_membership",
            "continuation",
            start="2026-09-01T00:00:00Z",
            student_source_key="student",
            class_source_key="class",
            school_relationship_source_key="other-relation",
            membership_status="assigned",
        )
    )
    result = coordinator.resolve_change(
        actor=actor,
        payload=EducationBatchInput(
            fact_kind="student_school_relationship",
            target_ref="f3:relationship:relation",
            operation="end",
            fields={},
            reason="正式结束期间",
        ),
        request_id=uuid4(),
        effective_at=at,
    )
    changes = {c.source_key: c for c in result.change_set.changes}
    assert set(changes) == {"relation", "member", "teaching"}
    assert changes["relation"].operation == "correct"
    assert len(changes["relation"].replacement_segments) == 1
    assert changes["relation"].replacement_segments[0].valid_to == at
    assert result.summary.operation == "end"


def test_residence_mode_only_does_not_require_more_education_reads():
    from app.composition.school_fact_education_product import EducationBatchInput

    coordinator, actor, _, _, _ = lifecycle_environment()
    coordinator.repository.read_records = lambda *a, **kw: pytest.fail(
        "unchanged enrollment broadened read scope"
    )
    result = coordinator.resolve_change(
        actor=actor,
        payload=EducationBatchInput(
            fact_kind="student_school_relationship",
            target_ref="f3:relationship:relation",
            operation="upsert",
            fields={"residence_mode": None},
            reason="寄宿方式未知",
        ),
        request_id=uuid4(),
        effective_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert len(result.change_set.changes) == 1


def test_short_historical_gap_never_silently_revives_education_arrangements():
    from app.composition.school_fact_education_product import EducationBatchInput

    coordinator, actor, _, _, _ = lifecycle_environment()
    at = datetime(2026, 8, 1, tzinfo=UTC)
    result = coordinator.resolve_change(
        actor=actor,
        payload=EducationBatchInput(
            fact_kind="student_school_relationship",
            target_ref="f3:relationship:relation",
            operation="correct",
            fields={"enrollment_status": "withdrawn", "valid_to": "2026-08-15T00:00:00Z"},
            reason="核正短期离校",
        ),
        request_id=uuid4(),
        effective_at=at,
    )
    for change in result.change_set.changes:
        if change.fact_kind != "student_school_relationship":
            assert len(change.replacement_segments) == 1
            assert change.replacement_segments[0].valid_to == at


@pytest.mark.parametrize(
    "kind", ["student_class_membership", "student_class_position", "teaching_group_member"]
)
def test_missing_complete_original_owner_read_rejects_ssr_before_a_proposal(kind):
    from app.composition.school_fact_education_product import EducationBatchInput

    coordinator, actor, _, _, _ = lifecycle_environment()
    original = coordinator.repository.read_records

    def read(*args, **kwargs):
        if args[1] == kind:
            raise PermissionError("complete dependency scope missing")
        return original(*args, **kwargs)

    coordinator.repository.read_records = read
    with pytest.raises(PermissionError):
        coordinator.resolve_change(
            actor=actor,
            payload=EducationBatchInput(
                fact_kind="student_school_relationship",
                target_ref="f3:relationship:relation",
                operation="end",
                fields={},
                reason="核正离校",
            ),
            request_id=uuid4(),
            effective_at=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_waiting_summary_rechecks_original_ssr_version_and_permission():
    from app.composition.school_fact_education_product import EducationBatchInput

    coordinator, actor, relation, _, _ = lifecycle_environment()
    result = coordinator.resolve_change(
        actor=actor,
        payload=EducationBatchInput(
            fact_kind="student_school_relationship",
            target_ref="f3:relationship:relation",
            operation="end",
            fields={},
            reason="核正离校",
        ),
        request_id=uuid4(),
        effective_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    relation["version"] = 2
    with pytest.raises(ValueError):
        result.summary_revalidate(actor)


def test_terminal_ssr_rejects_fields_instead_of_silently_ignoring_them():
    from app.composition.school_fact_education_product import EducationBatchInput

    coordinator, actor, _, _, _ = lifecycle_environment()
    with pytest.raises(ValueError):
        coordinator.resolve_change(
            actor=actor,
            payload=EducationBatchInput(
                fact_kind="student_school_relationship",
                target_ref="f3:relationship:relation",
                operation="end",
                fields={"enrollment_status": "enrolled"},
                reason="核正离校",
            ),
            request_id=uuid4(),
            effective_at=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_residence_correct_contract_keeps_exact_existing_ssr_and_reserved_fields():
    from app.composition.school_fact_residence_gateway import ResidencePrepareInput

    valid = dict(
        fact_kind="student_school_relationship",
        target_ref="f3:relationship:relation",
        operation="correct",
        fields={"residence_mode": None},
        reason="正式核正",
    )
    assert ResidencePrepareInput(**valid).operation == "correct"
    for altered in (
        {**valid, "fields": {"scope_id": "fake"}},
        {**valid, "target_ref": None, "creation_ref": "f3create:relationship:extra"},
    ):
        with pytest.raises(ValueError):
            ResidencePrepareInput(**altered)


@pytest.mark.parametrize(
    "operation,fields",
    [("end", {}), ("revoke", {}), ("correct", {"enrollment_status": "withdrawn"})],
)
def test_original_education_and_real_residence_coordinators_keep_one_complete_batch(
    operation, fields
):
    from unittest.mock import Mock

    from app.composition.school_fact_residence_maintenance import ResidenceLifecycleCoordinator

    coordinator, actor, relation, _, _ = lifecycle_environment()
    family = coordinator.family.repository
    family.read_residence_eligibility = lambda **kw: dict(
        school_fact_version=3, timelines=[relation], revalidate=lambda actor: None
    )
    family.reference_for = lambda **kw: "f3:relationship:relation"
    coordinator.family.adapter = SimpleNamespace(
        resolve_change=lambda **kw: pytest.fail("wrong owner")
    )
    repository = Mock()
    proof = Mock()
    repository.read_student_impacts.return_value = SimpleNamespace(
        school_fact_version=3,
        proof=proof,
        records=[
            dict(
                source_key="residence",
                version=1,
                bed_source_key="bed",
                valid_from="2026-01-01T00:00:00Z",
                valid_to=None,
            )
        ],
    )
    repository.read.return_value = SimpleNamespace(
        school_fact_version=3,
        proof=proof,
        records=[
            dict(fields={"student_name": "合成学生", "bed_name": "1号床", "room_name": "301室"})
        ],
    )
    repository.reference_for.return_value = "f6:student_bed_residence:residence"
    lifecycle = ResidenceLifecycleCoordinator(
        repository=repository,
        adapter=SimpleNamespace(public_fields=lambda actor, fields: fields),
        family_repository=family,
    )
    adapter = ResidenceEligibilityAdapter(
        education=SimpleNamespace(maintenance=coordinator, family=coordinator.family),
        lifecycle=lifecycle,
    )
    at = datetime(2026, 9, 1, tzinfo=UTC)
    result = adapter.resolve_change(
        actor=actor,
        payload=ResidenceBatchInput(
            fact_kind="student_school_relationship",
            target_ref="f3:relationship:relation",
            operation=operation,
            fields=fields,
            reason="正式核正",
        ),
        request_id=uuid4(),
        effective_at=at,
    )
    assert {c.source_key for c in result.change_set.changes} == {
        "relation",
        "member",
        "position",
        "teaching",
        "residence",
    }
    assert len(result.summary.impacts) == 5 and result.summary.operation == operation
    stay = next(c for c in result.change_set.changes if c.source_key == "residence")
    assert stay.operation == "end" and stay.effective_at == at
    result.summary_revalidate(actor)
    assert proof.revalidate.call_count >= 2


def test_unchanged_education_segments_keep_their_causal_history():
    from app.composition.school_fact_education_product import EducationBatchInput

    coordinator, actor, _, records, _ = lifecycle_environment()
    row = records[0]
    row["segments"][0].update(
        valid_to="2026-06-01T00:00:00Z", cause_source_key="old-approved-cause", cause_version=2
    )
    row["segments"].append(
        dict(
            effective_at="2026-06-01T00:00:00Z",
            valid_to=None,
            content=row["segments"][0]["content"],
        )
    )
    result = coordinator.resolve_change(
        actor=actor,
        payload=EducationBatchInput(
            fact_kind="student_school_relationship",
            target_ref="f3:relationship:relation",
            operation="end",
            fields={},
            reason="正式核正",
        ),
        request_id=uuid4(),
        effective_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    change = next(c for c in result.change_set.changes if c.source_key == "member")
    assert change.replacement_segments[0].cause_source_key == "old-approved-cause"
    assert change.replacement_segments[0].cause_version == 2
    assert change.replacement_segments[1].cause_source_key == "relation"
