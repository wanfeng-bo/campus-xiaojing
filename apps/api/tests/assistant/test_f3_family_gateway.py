from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.composition.school_fact_family_gateway import FamilyProfileInput, FamilyProfileToolGateway
from app.domains.students.family_read import FamilyQueryResult, FamilyReadEvidence
from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
from tests.test_f3_product_identity import guardian


def environment():
    actor = guardian()
    now = datetime.now(UTC)
    state = SimpleNamespace(current=True, valid=True)
    result = FamilyQueryResult(
        "available",
        {"name": "合成孩子"},
        (),
        (),
        now,
        now,
        "verified_guardian",
        evidence=FamilyReadEvidence(lambda candidate: state.valid and candidate == actor),
    )

    def identity(ref):
        if not state.current or ref != "identity":
            raise AssistantRuntimeError("identity changed")
        return SimpleNamespace(actor=actor)

    query = Mock()
    query.query.return_value = result
    owner = FamilyProfileToolGateway(resolve_actor=identity, query=query)
    context = ToolCallContext("identity", "conversation", "turn", "run", 1, uuid4())
    return owner.for_turn("identity"), context, query, state


def call(arguments=None):
    return ModelToolCall(
        call_id="c",
        name="school_facts.family.profile",
        arguments={"fields": ["name"]} if arguments is None else arguments,
    )


def test_selected_child_is_server_context_and_history_checks_current_evidence():
    gateway, context, query, state = environment()
    result = gateway.execute(call(), context)
    assert result.model_content["profile"] == {"name": "合成孩子"}
    assert result.history_check.valid()
    query.query.assert_called_once_with(actor=guardian(), as_of=None)
    state.valid = False
    assert result.history_check.valid() is False
    with pytest.raises(AssistantRuntimeError):
        gateway.revalidate("identity")


def test_f7_family_profile_sends_only_selected_quality_risk_to_model():
    gateway, context, query, _ = environment()
    query.quality_enabled = True
    query.query.return_value = replace(
        query.query.return_value,
        profile={"name": "合成孩子", "enrollment_status": "enrolled"},
        quality={
            "name": {"status": "no_current_claims", "risks": []},
            "enrollment_status": {
                "status": "claims", "risks": ["学校登记了这项资料的争议"],
                "details": [{"evidence_reference": "private-review-record"}],
            },
        },
        quality_version=7,
    )
    result = gateway.execute(call({"fields": ["enrollment_status"]}), context)
    query.query.assert_called_once_with(
        actor=guardian(), as_of=None, quality_fields=("enrollment_status",)
    )
    assert result.model_content["supporting_evidence"]["quality"] == {
        "enrollment_status": {
            "status": "claims", "risks": ["学校登记了这项资料的争议"]
        }
    }
    assert "private-review-record" not in str(result.model_content)
    assert "quality_version" not in result.model_content
    assert "name" not in str(result.model_content["supporting_evidence"]["quality"])


@pytest.mark.parametrize("field", ["student_id", "school_id", "fields", "relationship_id"])
def test_model_cannot_select_another_child_or_expand_projection(field):
    gateway, context, query, _ = environment()
    with pytest.raises(ValidationError):
        gateway.execute(call({field: "untrusted"}), context)
    query.query.assert_not_called()


def test_identity_switch_and_logout_fail_before_read_and_result_reuse():
    gateway, context, query, state = environment()
    with pytest.raises(AssistantRuntimeError):
        gateway.revalidate("other-identity")
    query.query.assert_not_called()
    result = gateway.execute(call(), context)
    state.current = False
    assert result.history_check.valid() is False
    with pytest.raises(AssistantRuntimeError):
        gateway.execute(call(), context)


def test_denial_has_no_private_card_or_reusable_history():
    gateway, context, query, _ = environment()
    now = datetime.now(UTC)
    query.query.return_value = FamilyQueryResult("denied", {}, (), (), now, now)
    result = gateway.execute(call(), context)
    assert result.model_content["status"] == "denied"
    assert result.history_execution_note == "read_returned_no_values"
    assert result.history_check is None
    assert result.fact_card["profile"] == {}


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "available"},
        {"profile": {"name": "returned-name"}},
        {"sources": ({"label": "returned-source"},)},
        {"missing": ("name",)},
        {"relationship_kind": "verified_guardian"},
    ],
)
def test_family_profile_unknown_or_partial_value_shape_is_not_no_values_history(overrides):
    gateway, context, query, _ = environment()
    now = datetime.now(UTC)
    query.query.return_value = replace(
        FamilyQueryResult("denied", {}, (), (), now, now), **overrides
    )
    result = gateway.execute(call(), context)
    assert result.history_execution_note is None


@pytest.mark.parametrize("operation", ["education", "teaching"])
@pytest.mark.parametrize(
    ("public", "expected"),
    [
        ({"status": "denied"}, "read_returned_no_values"),
        ({"status": "denied", "items": [{"name": "returned-name"}]}, None),
        ({"status": "incomplete", "items": [{"name": "returned-name"}]}, None),
        ({"status": "source_unavailable"}, None),
    ],
)
def test_family_relation_read_note_requires_exact_owner_no_values_outcome(
    operation, public, expected
):
    from copy import deepcopy

    from app.composition.school_fact_family_teaching_gateway import FamilyTeachingToolGateway

    actor = guardian()
    def identity(_):
        return SimpleNamespace(actor=actor)
    query = SimpleNamespace(query=lambda **_: SimpleNamespace(
        public=lambda: deepcopy(public), evidence=None,
    ))
    if operation == "education":
        owner = FamilyProfileToolGateway(resolve_actor=identity, query=Mock(), education=query)
        arguments = {"fields": ["teachers"]}
    else:
        owner = FamilyTeachingToolGateway(resolve_actor=identity, query=query)
        arguments = {}
    context = ToolCallContext("identity", "conversation", "turn", "run", 1, uuid4())
    result = owner.for_turn("identity").execute(ModelToolCall(
        call_id="family", name="school_facts.family." + operation, arguments=arguments,
    ), context)
    assert result.history_execution_note == expected
    assert result.history_check is None
    assert result.fact_card == public
    if expected:
        assert "returned-name" not in result.history_execution_note


def test_model_material_preserves_field_ownership_without_private_tracking_paths():
    gateway, context, query, _ = environment()
    query.query.return_value = replace(
        query.query.return_value,
        profile={
            "name": "另一同学",
            "campus": "湖畔校区",
            "region": {"district_name": "北城区", "district_code": "synthetic-north"},
        },
        sources=(
            {
                "fact": "person_region",
                "channel": "人工正式发布",
                "reference": "publication:internal",
                "source_trace_reference": "/engineering/source",
            },
        ),
    )
    result = gateway.execute(call({"fields": ["campus", "region"]}), context)
    assert result.model_content["profile"]["campus"] == "湖畔校区"
    assert result.model_content["field_meanings"]["region"] == "孩子的家庭居住地区"
    assert result.model_content["field_meanings"]["campus"] == "孩子在校关系中的主要校区名称"
    assert result.model_content["supporting_evidence"]["sources"] == [
        {"fact": "person_region", "channel": "人工正式发布"}
    ]
    assert result.fact_card["sources"][0]["source_trace_reference"] == "/engineering/source"
    assert result.fact_card["profile"]["region"]["district_name"] == "北城区"


@pytest.mark.parametrize(
    "status,label",
    [
        ("enrolled", "在校"),
        ("suspended", "休学"),
        ("graduated", "已毕业"),
        ("transferred_out", "已转出"),
        ("withdrawn", "已退学"),
    ],
)
def test_model_receives_domain_labels_but_canonical_card_keeps_values(status, label):
    gateway, context, query, _ = environment()
    query.query.return_value = replace(
        query.query.return_value,
        profile={
            "enrollment_status": status,
            "residence_mode": "day",
            "region": {"district_name": "另一地区", "district_code": "synthetic-code"},
        },
    )
    result = gateway.execute(
        call({"fields": ["enrollment_status", "residence_mode", "region"]}), context
    )
    assert result.model_content["profile"]["enrollment_status"] == label
    assert result.model_content["profile"]["residence_mode"] == "走读"
    assert result.model_content["profile"]["region"] == {"district_name": "另一地区"}
    assert result.fact_card["profile"]["enrollment_status"] == status
    assert result.fact_card["profile"]["region"]["district_code"] == "synthetic-code"


def test_model_schema_requires_explicit_fields_but_direct_http_input_remains_compatible():
    schema = FamilyProfileToolGateway.descriptors[0].parameters
    assert "fields" in schema["required"]
    assert set(schema["properties"]["fields"]["items"]["enum"]) == {
        "name",
        "enrollment_status",
        "campus",
        "student_number",
        "residence_mode",
        "region",
    }
    assert FamilyProfileInput.model_validate({}).as_of is None
    assert "fields" not in FamilyProfileInput.model_json_schema()["properties"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"fields": []},
        {"fields": "campus"},
        {"fields": ["home_address"]},
        {"fields": ["history"]},
        {"fields": ["campus", "guardian_phone"]},
        {"fields": ["campus"], "student_id": "other-child"},
        {"fields": ["campus"], "as_of": "2026-09-13T12:00:00"},
    ],
)
def test_invalid_model_field_selection_is_rejected_before_profile_read(arguments):
    gateway, context, query, _ = environment()
    with pytest.raises(ValidationError):
        gateway.execute(call(arguments), context)
    query.query.assert_not_called()


def sources(*facts):
    return tuple(
        {
            "fact": fact,
            "reference": "publication:" + fact,
            "published_at": "2026-09-13T01:00:00Z",
            "channel": "人工正式发布",
            "source_name": "source-for-" + fact,
            "source_trace_reference": "private-trace-" + fact,
        }
        for fact in facts
    )


def test_selected_school_fields_exclude_unasked_residential_values_from_model_and_card():
    gateway, context, query, state = environment()
    full = replace(
        query.query.return_value,
        profile={
            "name": "不需要重发的姓名",
            "enrollment_status": "enrolled",
            "campus": "湖畔校区",
            "student_number": "not-requested-number",
            "residence_mode": "boarding",
            "region": {"district_name": "不应混入校区的区县", "district_code": "synthetic"},
        },
        sources=sources("person", "student_school_relationship", "location", "person_region"),
    )
    query.query.return_value = full
    result = gateway.execute(call({"fields": ["enrollment_status", "campus"]}), context)
    assert result.model_content["profile"] == {"enrollment_status": "在校", "campus": "湖畔校区"}
    assert result.fact_card["profile"] == {"enrollment_status": "enrolled", "campus": "湖畔校区"}
    for output in (result.model_content, result.fact_card):
        assert set(output["authorized_fields"]) == {"enrollment_status", "campus"}
        source_values = output.get("supporting_evidence", output)["sources"]
        assert {source["fact"] for source in source_values} == {
            "student_school_relationship",
            "location",
        }
        assert "不应混入校区的区县" not in str(output)
        assert "not-requested-number" not in str(output)
    assert set(result.model_content["field_meanings"]) == {"enrollment_status", "campus"}
    assert full.profile["residence_mode"] == "boarding"
    assert len(full.sources) == 4
    state.valid = False
    assert result.history_check.valid() is False
    with pytest.raises(AssistantRuntimeError):
        gateway.revalidate("identity")


@pytest.mark.parametrize(
    "field,owners",
    [
        ("name", {"person"}),
        ("enrollment_status", {"student_school_relationship"}),
        ("campus", {"student_school_relationship", "location"}),
        ("student_number", {"student_school_relationship"}),
        ("residence_mode", {"student_school_relationship"}),
        ("region", {"person_region"}),
    ],
)
def test_each_selected_field_has_only_its_own_fact_sources(field, owners):
    gateway, context, query, _ = environment()
    query.query.return_value = replace(
        query.query.return_value,
        sources=sources("person", "student_school_relationship", "location", "person_region"),
    )
    result = gateway.execute(call({"fields": [field]}), context)
    source_values = result.model_content["supporting_evidence"]["sources"]
    assert {source["fact"] for source in source_values} == owners
    assert {source["fact"] for source in result.fact_card["sources"]} == owners


def test_field_selection_narrows_missing_and_conflicts_without_reclassifying_them():
    gateway, context, query, _ = environment()
    query.query.return_value = replace(
        query.query.return_value,
        status="conflict",
        profile={"enrollment_status": None, "campus": None, "region": None, "name": None},
        missing=("enrollment_status", "name"),
        conflicting_fields=("campus", "region"),
    )
    result = gateway.execute(call({"fields": ["enrollment_status", "campus"]}), context)
    for output in (result.model_content, result.fact_card):
        assert output["status"] == "conflict"
        assert output["missing"] == ["enrollment_status"]
        assert output["conflicting_fields"] == ["campus"]
        assert output["profile"] == {"enrollment_status": None, "campus": None}


def test_unrequested_conflict_does_not_mark_selected_available_field_as_conflicted():
    gateway, context, query, _ = environment()
    query.query.return_value = replace(
        query.query.return_value,
        status="conflict",
        profile={"campus": "湖畔校区", "region": None},
        conflicting_fields=("region",),
    )
    result = gateway.execute(call({"fields": ["campus"]}), context)
    for output in (result.model_content, result.fact_card):
        assert output["status"] == "available"
        assert output["conflicting_fields"] == []
        assert output["profile"] == {"campus": "湖畔校区"}


@pytest.mark.parametrize("conflicts", [(), ("unknown_private_field",)])
def test_unexplained_conflict_is_not_reclassified_as_available(conflicts):
    gateway, context, query, _ = environment()
    query.query.return_value = replace(
        query.query.return_value,
        status="conflict",
        profile={"campus": "湖畔校区"},
        conflicting_fields=conflicts,
    )
    result = gateway.execute(call({"fields": ["campus"]}), context)
    for output in (result.model_content, result.fact_card):
        assert output["status"] == "conflict"
        assert "unknown_private_field" not in str(output)


def test_requested_caregiver_denied_field_stays_restricted_without_other_fields_or_sources():
    gateway, context, query, _ = environment()
    query.query.return_value = replace(
        query.query.return_value,
        relationship_kind="recognized_caregiver",
        profile={"name": "照护孩子", "enrollment_status": "enrolled", "campus": "湖畔校区"},
        sources=sources("person", "student_school_relationship", "location"),
    )
    result = gateway.execute(call({"fields": ["enrollment_status", "student_number"]}), context)
    for output in (result.model_content, result.fact_card):
        assert set(output["profile"]) == {"enrollment_status"}
        assert output["authorized_fields"] == ["enrollment_status"]
        assert output["field_restrictions"] == {"student_number": "denied"}
        assert output["missing"] == []
        source_values = output.get("supporting_evidence", output)["sources"]
        assert {source["fact"] for source in source_values} == {"student_school_relationship"}
    denied_only = gateway.execute(call({"fields": ["student_number"]}), context)
    for output in (denied_only.model_content, denied_only.fact_card):
        assert output["profile"] == {}
        assert output["authorized_fields"] == []
        assert output.get("supporting_evidence", output)["sources"] == []
        assert output["field_restrictions"] == {"student_number": "denied"}


def history_read_environment(kind):
    from tests.school_facts.test_f4_family_education import fixture as education_fixture
    from tests.test_f3_family_read import setup

    service, actor, state = education_fixture() if kind == "education" else setup()
    state.selected_actor, state.resolves, state.authorities = actor, 0, 0
    authority = service.repository.authority

    def counted_authority(candidate):
        state.authorities += 1
        return authority(candidate)

    service.repository.authority = counted_authority

    def resolve(_):
        state.resolves += 1
        if state.selected_actor is None:
            raise AssistantRuntimeError("synthetic logout")
        return SimpleNamespace(actor=state.selected_actor)

    owner = FamilyProfileToolGateway(
        resolve_actor=resolve,
        query=service,
        education=service if kind == "education" else None,
    )
    turn = owner.for_turn("family")
    query_call = ModelToolCall(
        call_id="history",
        name="school_facts.family." + kind,
        arguments={"fields": ["class" if kind == "education" else "name"]},
    )
    context = ToolCallContext("family", "conversation", "turn", "run", 1, uuid4())
    result = turn.execute(query_call, context)
    assert result.history_check is not None
    state.calls = state.resolves = state.authorities = 0
    return turn, result.history_check, state, query_call, context


@pytest.mark.parametrize("kind", ["profile", "education"])
def test_family_history_reuses_one_complete_read_within_each_release(kind):
    from app.composition.school_fact_read_evidence import validation_batch

    turn, check, state, _, _ = history_read_environment(kind)
    with validation_batch():
        turn.revalidate("family")
        assert check.valid() and check.refresh()
        assert (state.resolves, state.calls, state.authorities) == (1, 0, 0)
    assert (state.resolves, state.calls, state.authorities) == (2, 1, 2)
    with validation_batch():
        assert check.valid()
    assert (state.resolves, state.calls, state.authorities) == (3, 2, 4)


@pytest.mark.parametrize("kind", ["profile", "education"])
def test_family_history_does_not_merge_distinct_evidence_for_same_selection(kind):
    from app.composition.school_fact_read_evidence import validation_batch

    turn, first, state, query_call, context = history_read_environment(kind)
    second = turn.execute(query_call, context).history_check
    state.calls = 0
    with validation_batch():
        turn.revalidate("family")
        assert first.valid() and second.valid()
    assert state.calls == 2


@pytest.mark.parametrize("kind", ["profile", "education"])
@pytest.mark.parametrize("change", ["child", "logout", "revoked", "boundary", "during_read"])
def test_family_history_release_rechecks_live_identity_authority_and_read_end(kind, change):
    from app.composition.school_fact_read_evidence import validation_batch
    from tests.test_f3_family_read import NOW

    _, check, state, _, _ = history_read_environment(kind)
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            assert check.valid()
            if change == "child":
                state.selected_actor = guardian(2)
            elif change == "logout":
                state.selected_actor = None
            elif change == "revoked":
                state.active = False
            elif change == "boundary":
                state.basis = replace(state.basis, valid_to=NOW)
            else:
                state.changed = True


@pytest.mark.parametrize("kind", ["profile", "education"])
def test_family_history_outside_release_is_immediate_and_not_cached(kind):
    _, check, state, _, _ = history_read_environment(kind)
    assert check.valid()
    assert (state.resolves, state.calls, state.authorities) == (1, 1, 2)
    state.active = False
    assert not check.refresh()
    assert state.resolves == 2
