from dataclasses import replace
from datetime import timedelta
from importlib import import_module

import pytest

from tests.test_f3_family_read import NOW, setup


def fixture(*, caregiver=False):
    old, actor, state = setup(caregiver=caregiver)
    state.payload = {
        "status": "no_current_records",
        "student_id": str(state.basis.student_id),
        "student_version": 1,
        "relationship_version": 1,
        "school_fact_version": 3,
        "records": [],
        "related_teachers": [],
        "locations": [],
        "official_contacts": [],
    }
    repo = old.repository

    def read(actor, *, factual_at, fields):
        state.calls += 1
        if state.changed:
            state.basis = None
        return dict(state.payload)

    repo.family_read = read
    module = import_module("app.composition.school_fact_family_education_query")
    service = module.FamilyEducationQueryService(
        repository=repo, current_actor=lambda a: state.active and a == actor, clock=lambda: NOW
    )
    return service, actor, state


def test_future_is_education_only_and_evidence_rechecks_current_relation():
    service, actor, state = fixture()
    at = NOW + timedelta(days=1)
    assert not state.basis.permits(actor, now=NOW, factual_at=at)
    result = service.query(actor=actor, fields=("class",), as_of=at)
    assert result.public()["status"] == "no_current_records"
    assert result.evidence.valid(actor)
    state.basis = None
    assert not result.evidence.valid(actor)


@pytest.mark.parametrize("condition", ["caregiver", "expired", "before", "other_child", "changed"])
def test_invalid_family_projection_never_discloses_records(condition):
    service, actor, state = fixture(caregiver=condition == "caregiver")
    at = NOW
    if condition == "expired":
        state.basis = replace(state.basis, valid_to=NOW)
    if condition == "before":
        at = NOW - timedelta(days=4)
    if condition == "other_child":
        state.payload["student_id"] = "00000000-0000-0000-0000-000000000999"
    if condition == "changed":
        state.changed = True
    result = service.query(actor=actor, fields=("class",), as_of=at)
    assert result.public()["status"] in {"denied", "unavailable"}
    assert "records" not in result.public()


def test_evidence_rechecks_education_content_version():
    service, actor, state = fixture()
    result = service.query(actor=actor, fields=("class",))
    state.payload["school_fact_version"] = 4
    assert not result.evidence.valid(actor)


def test_gateway_binds_selected_child_and_hides_internal_sources():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from uuid import uuid4

    from app.composition.school_fact_family_gateway import FamilyProfileToolGateway
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    service, actor, state = fixture()
    owner = FamilyProfileToolGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=actor), query=Mock(), education=service
    )
    turn = owner.for_turn("selected-child")
    call = ModelToolCall(
        call_id="c", name="school_facts.family.education", arguments={"fields": ["class"]}
    )
    context = ToolCallContext("selected-child", "c", "t", "r", 1, uuid4())
    result = turn.execute(call, context)
    assert result.model_content["profile"] == {"class": []}
    assert result.model_content["sources"] == []
    assert result.history_check.valid()
    state.active = False
    assert not result.history_check.valid()


def test_education_model_input_cannot_choose_child():
    from pydantic import ValidationError

    from app.composition.school_fact_family_gateway import FamilyEducationInput

    with pytest.raises(ValidationError):
        FamilyEducationInput.model_validate_json('{"fields":["class"],"student_id":"other"}')


def test_education_sources_keep_fact_ownership_and_recording_time_without_internal_ids():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from uuid import uuid4

    from app.composition.school_fact_family_gateway import FamilyProfileToolGateway
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    service, actor, state = fixture()
    state.payload["status"] = "available"
    state.payload["records"] = [
        {
            "fact_kind": kind,
            "source_key": key,
            "segments": [
                {
                    "effective_at": (NOW - timedelta(days=1)).isoformat(),
                    "valid_to": None,
                    "recorded_at": NOW.isoformat(),
                    "content": content,
                    "source": {
                        "source_kind": "school_publication",
                        "source_ref": "internal-uri",
                        "source_key": key,
                        "batch_id": "secret-id",
                        "publication_reference": "internal-trace",
                    },
                }
            ],
        }
        for kind, key, content in [
            ("education_class", "class-internal", {"display_name": "Scoped class"}),
            (
                "student_class_membership",
                "membership-internal",
                {"class_source_key": "class-internal"},
            ),
        ]
    ]
    owner = FamilyProfileToolGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=actor), query=Mock(), education=service
    )
    result = owner.for_turn("selected-child").execute(
        ModelToolCall(
            call_id="c", name="school_facts.family.education", arguments={"fields": ["class"]}
        ),
        ToolCallContext("selected-child", "c", "t", "r", 1, uuid4()),
    )
    sources = result.model_content["sources"]
    assert {s["fact"] for s in sources} == {"education_class", "student_class_membership"}
    assert all(s["recorded_at"] == NOW.isoformat() for s in sources)
    assert all(s["source_kind"] == "school_publication" for s in sources)
    assert "internal" not in str(sources) and "secret-id" not in str(sources)
    assert result.history_check.valid()


@pytest.mark.parametrize(
    "status,cutoff,anchor,expected",
    [
        ("enrolled", None, None, True),
        ("suspended", NOW.isoformat(), None, False),
        ("enrolled", NOW.isoformat(), None, False),
        ("enrolled", NOW.isoformat(), (NOW - timedelta(days=1)).isoformat(), False),
        ("enrolled", NOW.isoformat(), NOW.isoformat(), True),
    ],
)
def test_personal_grade_requires_explicit_post_suspension_anchor(status, cutoff, anchor, expected):
    from app.composition.school_fact_family_education_query import personal_grade_is_resolved

    arrangement = {
        "enrollment_status": status,
        "effective_at": NOW.isoformat(),
        "last_suspended_until": cutoff,
    }
    assert personal_grade_is_resolved(arrangement, anchor) is expected


def test_sending_proof_invalidates_when_school_arrangement_changes():
    service, actor, state = fixture()
    state.payload["grade_arrangement"] = {
        "enrollment_status": "enrolled",
        "effective_at": NOW.isoformat(),
        "last_suspended_until": None,
    }
    result = service.query(actor=actor, fields=("grade",))
    assert result.evidence.valid(actor)
    state.payload["grade_arrangement"] = {
        "enrollment_status": "suspended",
        "effective_at": NOW.isoformat(),
        "last_suspended_until": None,
    }
    assert not result.evidence.valid(actor)


def test_family_gateway_uses_shared_academic_year_material_without_changing_http_card():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from uuid import uuid4

    from app.composition.school_fact_family_gateway import FamilyProfileToolGateway
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    service, actor, _ = fixture()
    public = {
        "profile": {"grade": [{"status": "known", "display_name": "初二", "academic_year": 2026}]},
        "sources": [],
        "coverage": "unknown",
    }
    service.query = Mock(return_value=SimpleNamespace(public=lambda: public, evidence=None))
    owner = FamilyProfileToolGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=actor), query=Mock(), education=service
    )
    result = owner.for_turn("child").execute(
        ModelToolCall(
            call_id="q", name="school_facts.family.education", arguments={"fields": ["grade"]}
        ),
        ToolCallContext("child", "c", "t", "r", 1, uuid4()),
    )
    assert result.model_content["profile"]["grade"][0]["academic_year"] == {
        "start_year": 2026,
        "label": "2026学年",
    }
    assert result.fact_card["profile"]["grade"][0]["academic_year"] == 2026
    assert public["profile"]["grade"][0]["academic_year"] == 2026
