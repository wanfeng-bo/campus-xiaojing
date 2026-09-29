"""F5 same-result gateway/history checks share only one synchronous release."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.composition.school_fact_family_leadership_gateway import FamilyLeadershipToolGateway
from app.composition.school_fact_family_teaching_gateway import FamilyTeachingToolGateway
from app.composition.school_fact_read_evidence import validation_batch
from app.domains.students.family_read import FamilyReadEvidence
from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
from tests.test_f3_product_identity import guardian


@pytest.fixture(params=[FamilyTeachingToolGateway, FamilyLeadershipToolGateway])
def prepared(request):
    actor = guardian()
    state = {"current": actor, "valid": True, "checks": 0, "version": 1, "now": 0}

    def valid(current):
        state["checks"] += 1
        return current == actor and state["valid"] and state["version"] == 1 and state["now"] < 10

    evidence = FamilyReadEvidence(valid)
    query = SimpleNamespace(
        query=lambda **_: SimpleNamespace(public=lambda: {"status": "available"}, evidence=evidence)
    )
    gateway = request.param(
        resolve_actor=lambda _: SimpleNamespace(actor=state["current"]), query=query
    )
    turn = gateway.for_turn("selected")
    context = ToolCallContext("selected", "conversation", "turn", "run", 1, uuid4())
    result = turn.execute(
        ModelToolCall(call_id="one", name=gateway.descriptors[0].name, arguments={}), context
    )
    return turn, result.history_check, state


def test_gateway_and_history_share_exact_result_only_until_scope_exit(prepared):
    turn, history, state = prepared
    with validation_batch():
        turn.revalidate("selected")
        assert history.valid()
        assert history.refresh()
    assert state["checks"] == 1
    with validation_batch():
        turn.revalidate("selected")
        assert history.valid()
    assert state["checks"] == 2


@pytest.mark.parametrize("change", ["revoke", "version", "natural_boundary", "logout", "none"])
def test_change_after_queued_check_is_rejected_before_release(prepared, change):
    turn, history, state = prepared
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            turn.revalidate("selected")
            assert history.valid()
            if change == "logout":
                state["current"] = None
            elif change == "version":
                state["version"] = 2
            elif change == "natural_boundary":
                state["now"] = 10
            elif change == "none":
                state["valid"] = None
            else:
                state["valid"] = False


def test_outside_trusted_scope_is_immediate_and_next_wait_boundary_is_fresh(prepared):
    turn, history, state = prepared
    turn.revalidate("selected")
    assert state["checks"] == 1
    assert history.valid()
    assert state["checks"] == 2
    state["valid"] = False
    assert history.valid() is False
    assert state["checks"] == 3


@pytest.mark.parametrize("value", [1, "yes", object()])
def test_family_evidence_rejects_truthy_non_boolean(value):
    assert FamilyReadEvidence(lambda _: value).valid(guardian()) is False


@pytest.mark.parametrize("owner", ["profile", "education", "teaching", "leadership"])
def test_family_owner_relation_deadline_is_checked_after_final_identity(owner):
    from dataclasses import replace
    from datetime import timedelta

    from app.composition.school_fact_read_evidence import validate_fallback_read
    from tests.school_facts.test_f4_family_education import fixture as education
    from tests.school_facts.test_f5_family_leadership import fixture as leadership
    from tests.school_facts.test_f5_family_teaching import fixture as teaching
    from tests.test_f3_family_read import NOW, setup

    service, actor, state = {
        "profile": setup,
        "education": education,
        "teaching": teaching,
        "leadership": leadership,
    }[owner]()
    deadline = NOW + timedelta(seconds=1)
    state.basis = replace(state.basis, valid_to=deadline)
    state.clock = NOW
    service.clock = lambda: state.clock
    result = service.query(actor=actor, **({"fields": ("class",)} if owner == "education" else {}))
    assert result.evidence is not None
    with pytest.raises(AssistantRuntimeError, match="time boundary"):
        with validation_batch() as batch:
            batch.identity_checks["synthetic"] = SimpleNamespace(
                revalidate=lambda: setattr(state, "clock", deadline)
            )
            validate_fallback_read(
                owner, lambda: result.evidence.valid(actor), time_window=result.evidence.time_window
            )
    assert not batch.time_windows


@pytest.mark.parametrize("fixed", [False, True])
@pytest.mark.parametrize(
    "field", ["effective_at", "valid_to", "verified_at", "review_due_at", "segment"]
)
def test_family_education_known_fact_boundaries_only_follow_current_queries(fixed, field):
    from contextlib import nullcontext
    from datetime import timedelta

    from app.composition.school_fact_read_evidence import validate_fallback_read
    from tests.school_facts.test_f4_family_education import fixture
    from tests.test_f3_family_read import NOW

    service, actor, state = fixture()
    deadline = NOW + timedelta(seconds=1)
    state.clock = NOW
    service.clock = lambda: state.clock
    if field == "segment":
        state.payload["records"] = [
            {
                "source_key": "class:a",
                "fact_kind": "education_class",
                "segments": [
                    {
                        "effective_at": (NOW - timedelta(days=1)).isoformat(),
                        "valid_to": deadline.isoformat(),
                        "content": {"display_name": "Class"},
                    }
                ],
            }
        ]
    else:
        state.payload["quality"] = [
            {
                "field": "class",
                "quality_kind": "coverage",
                "coverage_status": "complete",
                "effective_at": (NOW - timedelta(days=1)).isoformat(),
                "verified_at": NOW.isoformat(),
                field: deadline.isoformat(),
            }
        ]
    result = service.query(actor=actor, fields=("class",), as_of=NOW if fixed else None)
    assert result.evidence is not None
    with nullcontext() if fixed else pytest.raises(AssistantRuntimeError, match="time boundary"):
        with validation_batch() as batch:
            batch.identity_checks["synthetic"] = SimpleNamespace(
                revalidate=lambda: setattr(state, "clock", deadline)
            )
            validate_fallback_read(
                "education",
                lambda: result.evidence.valid(actor),
                time_window=result.evidence.time_window,
            )


def test_family_leadership_known_review_deadline_is_checked_after_final_identity():
    from datetime import timedelta

    from app.composition.school_fact_read_evidence import validate_fallback_read
    from tests.school_facts.test_f5_family_leadership import fixture
    from tests.test_f3_family_read import NOW

    service, actor, state = fixture()
    deadline = NOW + timedelta(seconds=1)
    state.clock = NOW
    service.clock = lambda: state.clock
    state.payload["quality"] = [{"quality_kind": "coverage", "review_due_at": deadline.isoformat()}]
    result = service.query(actor=actor)
    assert result.evidence is not None
    with pytest.raises(AssistantRuntimeError, match="time boundary"):
        with validation_batch() as batch:
            batch.identity_checks["synthetic"] = SimpleNamespace(
                revalidate=lambda: setattr(state, "clock", deadline)
            )
            validate_fallback_read(
                "leadership",
                lambda: result.evidence.valid(actor),
                time_window=result.evidence.time_window,
            )
