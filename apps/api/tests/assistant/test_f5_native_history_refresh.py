"""Formal PydanticAI recovers from stale F5 history before releasing any old facts."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic_ai")
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from app.composition.school_fact_directory_gateway import CombinedFactTurnGateway
from app.composition.school_fact_family_teaching_gateway import FamilyTeachingToolGateway
from app.domains.students.family_read import FamilyReadEvidence
from app.platform.assistant.pydanticai_runner import CandidateToolRunner
from app.platform.assistant.runtime import AssistantRuntime, AssistantRuntimeError, HistoryCheck
from app.platform.assistant.tool_loop import ToolDescriptor, ToolExecutionResult
from tests.test_f3_product_identity import guardian


@pytest.mark.parametrize(
    "change", ["new_fact", "natural_boundary", "logout", "revoked", "revoked_during_refresh"]
)
def test_changed_native_f5_history_does_not_block_authorized_fresh_query(change):
    state = {"version": 1, "time": 0, "allowed": True}
    actor = guardian()

    def identity(_):
        if not state["allowed"]:
            raise AssistantRuntimeError("current identity unavailable")
        return SimpleNamespace(actor=actor)

    def query(**kwargs):
        version, at = state["version"], state["time"]
        label = "OLD_TEACHER_PRIVATE" if version == 1 and at == 0 else "NEW_TEACHER"

        def valid(current):
            if state.get("revoke_during_refresh"):
                state["allowed"] = False
            return (
                current == actor
                and state["allowed"]
                and state["version"] == version
                and state["time"] == at
            )

        return SimpleNamespace(
            public=lambda: {"status": "available", "items": [{"teacher": label}]},
            evidence=FamilyReadEvidence(valid),
        )

    owner = FamilyTeachingToolGateway(resolve_actor=identity, query=SimpleNamespace(query=query))
    seen = []

    async def answer(messages, _info):
        seen.append(str(messages))
        if len(seen) % 2:
            return ModelResponse(parts=[ToolCallPart("tool_0", {}, "call-" + str(len(seen)))])
        return ModelResponse(parts=[TextPart("正式查询完成")])

    runner = CandidateToolRunner(
        model_factory=lambda _: FunctionModel(answer),
        gateway_factory=lambda ref: CombinedFactTurnGateway(owner.for_turn(ref)),
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("identity", uuid4())

    def turn():
        accepted = runtime.accept_turn(
            conversation.conversation_ref, "identity", uuid4(), "下学期数学是谁教？"
        )
        return asyncio.run(
            runner.run_accepted(
                runtime=runtime, accepted=accepted, actor_context_ref="identity", request_id=uuid4()
            )
        )

    turn()
    if change == "new_fact":
        state["version"] = 2
    elif change == "natural_boundary":
        state["time"] = 1
    elif change == "revoked_during_refresh":
        state["revoke_during_refresh"] = True
    else:
        state["allowed"] = False
    if not state["allowed"] or change == "revoked_during_refresh":
        with pytest.raises(AssistantRuntimeError):
            turn()
        assert len(seen) == 2
    else:
        result = turn()
        assert len(seen) == 4
        assert "OLD_TEACHER_PRIVATE" not in seen[2]
        assert "history_context_unavailable" in seen[2]
        assert "NEW_TEACHER" in str(result.display_ledger[-1].display_payload)


class CandidateGateway:
    """Generic owner evidence; no school query or production ordinal selection."""

    descriptors: tuple[ToolDescriptor, ...] = (
        ToolDescriptor(
            name="objects.read",
            owner="objects",
            kind="read",
            description="Read authorized objects",
            parameters={
                "type": "object",
                "properties": {"reference": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
    )

    def __init__(self):
        self.candidates = [
            {"reference": "object-a", "name": "North room"},
            {"reference": "object-b", "name": "South room"},
        ]
        self.live = True
        self.proof_current = True
        self.version = 1
        self.time = 0
        self.failure: str | None = None
        self.calls = []

    def revalidate(self, _):
        if not self.live:
            raise AssistantRuntimeError("current identity unavailable")

    def execute(self, call, _):
        self.calls.append(deepcopy(call.arguments))
        if self.failure:
            return ToolExecutionResult(
                model_content={"status": self.failure, "detail": "UNVERIFIED_FAILURE_MATERIAL"},
                history_execution_note="read_returned_no_values",
            )
        candidates, version, at = deepcopy(self.candidates), self.version, self.time
        reference = call.arguments.get("reference")
        material = (
            {"status": "available", "reference": reference}
            if reference is not None
            else {"status": "candidates", "items": candidates}
        )
        return ToolExecutionResult(
            model_content=material,
            history_check=HistoryCheck(
                lambda: (
                    self.proof_current
                    and self.candidates == candidates
                    and self.version == version
                    and self.time == at
                ),
                key="object-list",
            ),
        )


def _history_turn(runtime, conversation, gateway, text, responses, seen, **runner_options):
    def model_factory(current):
        async def respond(messages, _):
            current()
            seen.append(deepcopy(messages))
            response = responses.pop(0)
            result = response(messages) if callable(response) else response
            assert isinstance(result, ModelResponse)
            return result

        return FunctionModel(respond)

    accepted = runtime.accept_turn(conversation, "owner", uuid4(), text)
    return asyncio.run(
        CandidateToolRunner(
            model_factory=model_factory, gateway_factory=lambda _: gateway, **runner_options
        ).run_accepted(
            runtime=runtime, accepted=accepted, actor_context_ref="owner", request_id=uuid4()
        )
    )


def _object_call(reference=None):
    return ModelResponse(
        parts=[ToolCallPart("tool_0", {} if reference is None else {"reference": reference})]
    )


def _object_answer(text="Available objects", *, overflow=False):
    parts: list[TextPart | ThinkingPart] = [TextPart(text)]
    if overflow:
        parts.insert(0, ThinkingPart("SYNTHETIC_PRIVATE_THOUGHT_" * 1000))
    return ModelResponse(parts=parts)


def _failed_selection(*, overflow=False, status="denied", runtime=None):
    runtime = runtime or AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    gateway, seen = CandidateGateway(), []
    _history_turn(
        runtime,
        conversation,
        gateway,
        "Which matching rooms?",
        [_object_call(), _object_answer(overflow=overflow)],
        seen,
    )
    gateway.failure = status
    failed = _history_turn(
        runtime,
        conversation,
        gateway,
        "The second one",
        [_object_call("object-b"), _object_answer("UNVERIFIED_FAILURE_ANSWER")],
        seen,
    )
    assert failed.model_safe_ledger[-1].model_history_checks is None
    gateway.failure = None
    return runtime, conversation, gateway, seen


@pytest.mark.parametrize("case", ["revoked", "capacity", "missing_proof", "empty_verified"])
def test_quoted_output_omission_is_unknown_history_not_a_past_fact(case):
    from app.platform.assistant.pydanticai_history import recover_quoted_history
    from app.platform.assistant.runtime import ModelSafeLedgerEntry

    check = HistoryCheck(lambda: case != "revoked")
    entry = ModelSafeLedgerEntry(
        turn_ref="turn",
        run_ref="run",
        generation=1,
        teacher_utterance="original request",
        technical_marker="completed",
        model_safe_template="PRIVATE_ANSWER" + ("x" * 2000 if case == "capacity" else ""),
        model_safe_note=json.dumps([{"status": "no_current_records", "items": []}]),
        model_history_checks=None if case == "missing_proof" else (check,),
    )
    quoted, checks, unavailable = recover_quoted_history(
        [entry],
        checkpoint=lambda: None,
        max_bytes=1500,
    )
    turn = json.loads(quoted)["turns"][0]
    if case == "empty_verified":
        assert not unavailable and checks == (check,)
        assert turn["quoted_tool_results"] == [{"status": "no_current_records", "items": []}]
        assert "omitted_output_context" not in turn
    else:
        assert unavailable and checks == ()
        assert "PRIVATE_ANSWER" not in quoted
        assert "quoted_tool_results" not in turn
        assert turn["omitted_output_context"]["prior_completion"] == "unknown"
        assert turn["omitted_output_context"]["prior_specific_values"] == "unknown"


def test_mixed_success_and_unproved_failure_does_not_assert_all_failed_in_recovery():
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    gateway, seen = CandidateGateway(), []

    def fail_second(messages):
        gateway.failure = "denied"
        return _object_call("object-b")

    first = _history_turn(
        runtime,
        conversation,
        gateway,
        "two reads",
        [
            _object_call(),
            fail_second,
            _object_answer("PRIVATE_MIXED_ANSWER"),
        ],
        seen,
    )
    assert first.model_safe_ledger[-1].model_history_checks is None
    gateway.failure = None

    def inspect(messages):
        turn = _quoted_turns(messages)[0]
        assert turn["past_tool_executions"] == [
            {
                "owner": "objects",
                "tool": "objects.read",
                "call_index": 2,
                "outcome": "read_returned_no_values",
            }
        ]
        assert turn["omitted_output_context"]["prior_completion"] == "unknown"
        assert "quoted_assistant_text" not in turn
        assert turn["verified_tool_results"][0]["call_index"] == 1
        assert turn["verified_tool_results"][0]["result"]["items"] == gateway.candidates
        assert "PRIVATE_MIXED_ANSWER" not in str(messages)
        assert "North room" in str(messages)
        return _object_answer("Cannot infer the earlier outcome")

    _history_turn(runtime, conversation, gateway, "where did that come from", [inspect], seen)


@pytest.mark.parametrize("cutoff", [0, 2])
def test_owner_execution_note_survives_fact_capacity_but_not_generation_cutoff(cutoff):
    from app.platform.assistant.pydanticai_history import pack_history_note, recover_quoted_history
    from app.platform.assistant.runtime import ModelSafeLedgerEntry

    events = [
        {
            "owner": "objects",
            "tool": "objects.read",
            "call_index": 1,
            "outcome": "read_returned_no_values",
        }
    ]
    note = pack_history_note([{"secret": "PRIVATE" * 2000}], events)
    assert note is not None and "PRIVATE" not in note
    entry = ModelSafeLedgerEntry(
        turn_ref="turn",
        run_ref="run",
        generation=1,
        teacher_utterance="read request",
        technical_marker="completed",
        model_safe_note=note,
        model_safe_template="PRIVATE_ANSWER",
        model_history_checks=(HistoryCheck(lambda: True),),
    )
    quoted, checks, unavailable = recover_quoted_history(
        [entry],
        checkpoint=lambda: None,
        material_since_generation=cutoff,
    )
    turn = json.loads(quoted)["turns"][0]
    assert ("past_tool_executions" in turn) == (cutoff == 0)
    assert "PRIVATE" not in quoted and checks == () and unavailable


@pytest.mark.parametrize("mutation", ["version", "extra", "event_value", "event_secret"])
def test_unknown_execution_envelope_fails_closed(mutation):
    from app.platform.assistant.pydanticai_history import unpack_history_note

    value = {
        "history_note_version": 1,
        "results": [{"secret": "private"}],
        "past_tool_executions": [
            {
                "owner": "objects",
                "tool": "objects.read",
                "call_index": 1,
                "outcome": "read_returned_no_values",
            }
        ],
    }
    if mutation == "version":
        value["history_note_version"] = 2
    elif mutation == "extra":
        value["extra"] = "private"
    elif mutation == "event_value":
        value["past_tool_executions"][0]["outcome"] = "succeeded"
    else:
        value["past_tool_executions"][0]["number"] = "private"
    assert unpack_history_note(json.dumps(value)) == (None, [])


def test_nonread_descriptor_cannot_publish_a_read_execution_note():
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    gateway, seen = CandidateGateway(), []
    gateway.descriptors = (gateway.descriptors[0].model_copy(update={"kind": "prepare"}),)
    gateway.failure = "denied"
    first = _history_turn(
        runtime,
        conversation,
        gateway,
        "prepare",
        [
            _object_call(),
            _object_answer("UNVERIFIED_PREPARE"),
        ],
        seen,
    )
    assert first.model_safe_ledger[-1].model_history_checks is None
    assert isinstance(json.loads(first.model_safe_ledger[-1].model_safe_note), list)
    _history_turn(runtime, conversation, gateway, "what happened", [_object_answer()], seen)
    assert "past_tool_executions" not in str(seen[-1])
    assert "UNVERIFIED_PREPARE" not in str(seen[-1])


@pytest.mark.parametrize("revoke", [None, "known", "empty", "during_send"])
def test_mixed_empty_known_and_denied_recover_only_each_current_owner_projection(revoke):
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    gateway, seen = CandidateGateway(), []
    live = {"known": True, "empty": True}

    def execute(call, context):
        selected = call.arguments["reference"]
        if selected == "denied":
            return ToolExecutionResult(
                model_content={"status": "denied"}, history_execution_note="read_returned_no_values"
            )
        result = (
            {"status": "no_current_records", "scope": "selected range", "items": []}
            if selected == "empty"
            else {"status": "known", "name": "VERIFIED_NAME"}
        )
        return ToolExecutionResult(
            model_content={**result, "not_for_history": "PRIVATE_CURRENT_ONLY"},
            history_content=result,
            history_check=HistoryCheck(lambda: live[selected]),
        )

    gateway.execute = execute
    first = _history_turn(
        runtime,
        conversation,
        gateway,
        "read requested facts",
        [
            _object_call("empty"),
            _object_call("known"),
            _object_call("denied"),
            _object_answer("UNVERIFIED_WHOLE_ANSWER"),
        ],
        seen,
    )
    entry = first.model_safe_ledger[-1]
    assert entry.model_history_checks is None and len(entry.tool_history) == 2
    assert (
        len(entry.model_safe_note or "") + sum(len(i.material_json) for i in entry.tool_history)
        <= 8000
    )
    if revoke in live:
        live[revoke] = False

    def inspect(messages):
        turn = _quoted_turns(messages)[0]
        results = turn["verified_tool_results"]
        assert [r["call_index"] for r in results] == (
            [1] if revoke == "known" else [2] if revoke == "empty" else [1, 2]
        )
        if revoke != "empty":
            assert results[0]["result"] == {
                "status": "no_current_records",
                "scope": "selected range",
                "items": [],
            }
        assert "UNVERIFIED_WHOLE_ANSWER" not in str(messages)
        assert "PRIVATE_CURRENT_ONLY" not in str(messages)
        assert turn["past_tool_executions"][0]["call_index"] == 3
        if revoke == "during_send":
            live["known"] = False
        return _object_answer("Use only the verified results")

    if revoke == "during_send":
        with pytest.raises(AssistantRuntimeError):
            _history_turn(runtime, conversation, gateway, "follow up", [inspect], seen)
    else:
        next_turn = _history_turn(runtime, conversation, gateway, "follow up", [inspect], seen)
        assert len(next_turn.model_safe_ledger[-1].model_history_checks) == (1 if revoke else 2)


@pytest.mark.parametrize("cutoff", [0, 2])
def test_individual_proof_recovery_obeys_capacity_cutoff_and_check_limit(cutoff):
    from app.platform.assistant.pydanticai_history import recover_quoted_history
    from app.platform.assistant.runtime import ModelSafeLedgerEntry, ToolHistoryProjection

    first, second = HistoryCheck(lambda: True), HistoryCheck(lambda: True)
    entries = tuple(
        ToolHistoryProjection(json.dumps({"call_index": n, "result": {"value": "x" * 500}}), proof)
        for n, proof in [(1, first), (2, second)]
    )
    entry = ModelSafeLedgerEntry(
        turn_ref="turn",
        run_ref="run",
        generation=1,
        teacher_utterance="request",
        technical_marker="completed",
        model_safe_template="UNVERIFIED",
        tool_history=entries,
    )
    quoted, checks, _ = recover_quoted_history(
        [entry],
        checkpoint=lambda: None,
        material_since_generation=cutoff,
        max_checks=1,
        max_bytes=1800,
    )
    assert "UNVERIFIED" not in quoted
    turn = json.loads(quoted)["turns"][0]
    if cutoff:
        assert "verified_tool_results" not in turn and checks == ()
    else:
        assert [r["call_index"] for r in turn["verified_tool_results"]] == [1]
        assert checks == (first,)


def test_mixed_tool_history_does_not_bypass_total_ledger_material_capacity():
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    gateway, seen = CandidateGateway(), []

    def execute(call, context):
        ref = call.arguments["reference"]
        if ref == "denied":
            return ToolExecutionResult(
                model_content={"status": "denied"}, history_execution_note="read_returned_no_values"
            )
        return ToolExecutionResult(
            model_content={"value": "x" * 7900 if ref == "large" else "SMALL"},
            history_check=HistoryCheck(lambda: True),
        )

    gateway.execute = execute
    first = _history_turn(
        runtime,
        conversation,
        gateway,
        "request",
        [
            _object_call("large"),
            _object_call("small"),
            _object_call("denied"),
            _object_answer("unverified"),
        ],
        seen,
    )
    entry = first.model_safe_ledger[-1]
    assert (
        len(entry.model_safe_note or "") + sum(len(t.material_json) for t in entry.tool_history)
        <= 8000
    )
    assert len(entry.tool_history) == 1 and "SMALL" in entry.tool_history[0].material_json


@pytest.mark.parametrize("revoke", [None, "before_third", "during_third"])
def test_optional_final_review_once_keeps_only_published_answer_and_verified_context(revoke):
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    gateway, seen = CandidateGateway(), []
    instruction = "INTERNAL_REVIEW_INSTRUCTION"
    first = _history_turn(
        runtime,
        conversation,
        gateway,
        "read",
        [
            _object_call(),
            _object_answer("UNPUBLISHED_DRAFT"),
            _object_answer("PUBLISHED_FINAL"),
        ],
        seen,
        final_review_instruction=instruction,
        max_model_calls=3,
    )
    assert len(seen) == 3 and len(gateway.calls) == 1
    assert first.model_safe_ledger[-1].model_safe_template == "PUBLISHED_FINAL"
    assert "UNPUBLISHED_DRAFT" not in str(first.display_ledger)

    def inspect(messages):
        assert "PUBLISHED_FINAL" in str(messages) and "North room" in str(messages)
        assert "UNPUBLISHED_DRAFT" not in str(messages)
        assert instruction not in str(messages)
        return _object_answer("NEXT_DRAFT")

    _history_turn(
        runtime,
        conversation,
        gateway,
        "refer back",
        [inspect, _object_answer("NEXT_FINAL")],
        seen,
        final_review_instruction=instruction,
    )
    assert len(gateway.calls) == 1

    if revoke == "before_third":
        gateway.proof_current = False
    inspected = []

    def inspect_third(messages):
        material = str(messages)
        assert ("NEXT_FINAL" in material) == (revoke != "before_third")
        assert ("North room" in material) == (revoke != "before_third")
        assert "NEXT_DRAFT" not in material and "UNPUBLISHED_DRAFT" not in material
        assert instruction not in material
        if revoke == "during_third":
            gateway.proof_current = False
        inspected.append(True)
        return _object_answer("THIRD_DRAFT")

    def third():
        return _history_turn(
            runtime,
            conversation,
            gateway,
            "refer back again",
            [inspect_third, _object_answer("THIRD_FINAL")],
            seen,
            final_review_instruction=instruction,
        )

    if revoke == "during_third":
        with pytest.raises(AssistantRuntimeError):
            third()
        assert "THIRD_DRAFT" not in str(runtime.snapshot(conversation, "owner").display_ledger)
    else:
        third()
    assert inspected == [True]
    assert len(gateway.calls) == 1


def test_review_oversized_tool_material_is_not_mistaken_for_no_new_tool_material():
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    gateway, seen = CandidateGateway(), []
    options = {"final_review_instruction": "Review once"}
    _history_turn(
        runtime,
        conversation,
        gateway,
        "read",
        [_object_call(), _object_answer("FIRST_DRAFT"), _object_answer("FIRST_FINAL")],
        seen,
        **options,
    )
    gateway.execute = lambda call, context: ToolExecutionResult(
        model_content={"value": "x" * 8001},
        history_check=HistoryCheck(lambda: True),
    )
    second = _history_turn(
        runtime,
        conversation,
        gateway,
        "read another",
        [_object_call(), _object_answer("LARGE_DRAFT"), _object_answer("LARGE_FINAL")],
        seen,
        **options,
    )
    entry = second.model_safe_ledger[-1]
    assert entry.model_history_checks and entry.model_safe_note is None

    def inspect(messages):
        assert "LARGE_FINAL" not in str(messages)
        assert "FIRST_FINAL" in str(messages) and "North room" in str(messages)
        return _object_answer("THIRD_DRAFT")

    _history_turn(
        runtime,
        conversation,
        gateway,
        "refer back",
        [inspect, _object_answer("THIRD_FINAL")],
        seen,
        **options,
    )


@pytest.mark.parametrize("limit", [1, 2])
def test_final_review_uses_original_request_limit_without_releasing_draft(limit):
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    gateway, seen = CandidateGateway(), []
    responses = [_object_answer("DRAFT"), _object_answer("FINAL")]
    if limit == 1:
        with pytest.raises(AssistantRuntimeError):
            _history_turn(
                runtime,
                conversation,
                gateway,
                "answer",
                responses,
                seen,
                final_review_instruction="Review once",
                max_model_calls=limit,
            )
        assert len(seen) == 1
        assert "DRAFT" not in str(runtime.snapshot(conversation, "owner").display_ledger)
    else:
        result = _history_turn(
            runtime,
            conversation,
            gateway,
            "answer",
            responses,
            seen,
            final_review_instruction="Review once",
            max_model_calls=limit,
        )
        assert len(seen) == 2 and result.model_safe_ledger[-1].model_safe_template == "FINAL"


@pytest.mark.parametrize("oversized", [False, True])
def test_real_teaching_coordinates_survive_date_followup_for_partial_prepare(oversized):
    from app.composition.school_fact_education_maintenance import EducationMaintenanceCoordinator
    from app.composition.school_fact_education_product import (
        EducationBatchInput,
        EducationProductAdapter,
    )
    from app.composition.school_fact_teaching_gateway import StaffTeachingGateway
    from tests.school_facts.test_f5_staff_teaching_query import AT, actor, signed_service

    query, repo, source, external, _ = signed_service()
    identity = SimpleNamespace(actor=actor(), session_binding="session")
    owner = StaffTeachingGateway(resolve_actor=lambda _: identity, query=query)
    external.reference_for = lambda *, fact_kind, source_key, **kw: f"f2:{fact_kind}:{source_key}"
    external.source_for_reference = lambda *, reference, **kw: reference.split(":", 2)[2]
    coordinator = EducationMaintenanceCoordinator(
        EducationProductAdapter(repository=repo, external=external), family=None
    )
    read_descriptor = next(d for d in owner.descriptors if d.name.endswith(".query"))
    gateway, prepared, original = CandidateGateway(), [], []
    gateway.descriptors = (
        read_descriptor,
        ToolDescriptor(
            name="education.prepare",
            owner="education",
            kind="prepare",
            description="Prepare an existing education change",
            parameters=EducationBatchInput.model_json_schema(),
        ),
    )

    def execute(call, context):
        if call.name == read_descriptor.name:
            result = owner.execute(call, context)
            teacher = result.model_content["items"][0]["teachers"][0]
            original.append(deepcopy(teacher["tool_context"]))
            if oversized:
                result.model_content["capacity_test_padding"] = "x" * 8001
            return result
        payload = EducationBatchInput.model_validate(call.arguments)
        assert payload.fact_kind == original[0]["fact_kind"] == "teaching_assignment"
        assert payload.target_ref == original[0]["reference"]
        assert payload.fields == {"assignment_ref": "f2:assignment:assignment:b"}
        prepared.append(
            coordinator.resolve_change(
                actor=actor(), payload=payload, request_id=uuid4(), effective_at=AT
            )
        )
        return ToolExecutionResult(model_content={"status": "prepared"})

    gateway.execute = execute
    runtime, seen = AssistantRuntime(), []
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    options = {"final_review_instruction": "Review once"}
    _history_turn(
        runtime,
        conversation,
        gateway,
        "current teaching",
        [
            ModelResponse(
                parts=[ToolCallPart("tool_0", {"view": "teaching", "subject_name": "数学"})]
            ),
            _object_answer("TEACHING_DRAFT"),
            _object_answer("TEACHING_FINAL"),
        ],
        seen,
        **options,
    )
    second = _history_turn(
        runtime,
        conversation,
        gateway,
        "on which date",
        [
            _object_answer("DATE_DRAFT"),
            _object_answer("DATE_FINAL"),
        ],
        seen,
        **options,
    )
    assert second.model_safe_ledger[-1].model_safe_note == "[]"

    def select(messages):
        turns = _quoted_turns(messages)
        material = str(turns)
        assert "DATE_FINAL" in material
        assert "DATE_DRAFT" not in material and "TEACHING_DRAFT" not in material
        if oversized:
            assert original[0]["reference"] not in material
            assert "quoted_tool_results" not in turns[0]
            return _object_answer("Need a fresh authorized selection")
        teacher = turns[0]["quoted_tool_results"][0]["items"][0]["teachers"][0]
        assert teacher["tool_context"] == original[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "tool_1",
                    {
                        "fact_kind": teacher["tool_context"]["fact_kind"],
                        "target_ref": teacher["tool_context"]["reference"],
                        "fields": {"assignment_ref": "f2:assignment:assignment:b"},
                        "reason": "synthetic offline contract",
                    },
                )
            ]
        )

    responses = [select, _object_answer("PREPARE_FINAL")]
    if not oversized:
        responses.insert(1, _object_answer("PREPARE_DRAFT"))
    _history_turn(
        runtime, conversation, gateway, "change only the teacher", responses, seen, **options
    )
    assert len(prepared) == (0 if oversized else 1)
    if prepared:
        summary = prepared[0].summary
        assert summary.after == {**summary.before, "assignment_ref": "f2:assignment:assignment:b"}
        assert prepared[0].change_set.changes[0].source_key == "teaching:a"
    assert source.rows["teaching_assignment"][0]["version"] == 1


@pytest.mark.parametrize("attempt", ["read", "prepare", "load"])
def test_review_cannot_dispatch_tools_or_load_contracts_even_with_remaining_budget(attempt):
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    gateway, seen = CandidateGateway(), []
    if attempt == "prepare":
        gateway.descriptors = (gateway.descriptors[0].model_copy(update={"kind": "prepare"}),)
    elif attempt == "load":
        gateway.descriptors = (gateway.descriptors[0].model_copy(update={"load_on_demand": True}),)
    retry = (
        ModelResponse(parts=[ToolCallPart("search_tools", {"queries": ["tool_0"]}, "review-load")])
        if attempt == "load"
        else _object_call()
    )
    with pytest.raises(AssistantRuntimeError):
        _history_turn(
            runtime,
            conversation,
            gateway,
            "answer",
            [_object_answer("DRAFT"), retry],
            seen,
            final_review_instruction="Review once",
            max_model_calls=8,
            max_tool_calls=12,
        )
    assert len(seen) == 2 and gateway.calls == []
    assert "DRAFT" not in str(runtime.snapshot(conversation, "owner").display_ledger)


@pytest.mark.parametrize("failure", ["permission", "cancel", "deadline"])
def test_review_keeps_original_current_proof_cancellation_and_deadline(failure):
    import time

    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    gateway, seen = CandidateGateway(), []

    def last(messages):
        if failure == "permission":
            gateway.proof_current = False
        elif failure == "cancel":
            active = runtime.snapshot(conversation, "owner").active_run
            runtime.stop_run(conversation, "owner", active.run_ref, active.generation, uuid4())
        else:
            time.sleep(0.04)
        return _object_answer("MUST_NOT_PUBLISH")

    with pytest.raises(AssistantRuntimeError):
        _history_turn(
            runtime,
            conversation,
            gateway,
            "answer",
            [
                _object_call(),
                _object_answer("DRAFT"),
                last,
            ],
            seen,
            final_review_instruction="Review once",
            timeout_seconds=0.03 if failure == "deadline" else 180,
        )
    assert len(gateway.calls) == 1
    displayed = str(runtime.snapshot(conversation, "owner").display_ledger)
    assert "MUST_NOT_PUBLISH" not in displayed and "DRAFT" not in displayed


def _quoted_turns(messages):
    return next(
        json.loads(part.content)["turns"]
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
        and isinstance(part.content, str)
        and part.content.startswith('{"kind":"quoted_historical_user_intents"')
    )


@pytest.mark.parametrize("status", ["denied", "source_unavailable"])
@pytest.mark.parametrize("overflow", [False, True])
def test_completed_failed_selection_recovers_verified_mapping_for_model_retry(status, overflow):
    runtime, conversation, gateway, seen = _failed_selection(overflow=overflow, status=status)
    retry_start = len(seen)

    def choose_second(messages):
        turns = _quoted_turns(messages)
        assert turns[1]["quoted_user_text"] == "The second one"
        assert "quoted_tool_results" not in turns[1]
        assert "quoted_assistant_text" not in turns[1]
        assert turns[1]["past_tool_executions"] == [
            {
                "owner": "objects",
                "tool": "objects.read",
                "call_index": 1,
                "outcome": "read_returned_no_values",
            }
        ]
        assert turns[1]["omitted_output_context"]["prior_specific_values"] == "unknown"
        assert turns[1]["omitted_output_context"]["prior_completion"] == "unknown"
        assert turns[1]["omitted_output_context"]["retained_input"] == "user_request_only"
        assert "UNVERIFIED_FAILURE" not in str(messages)
        assert "ToolCallPart" not in str(messages)  # Failed arguments are not authority.
        if "quoted_tool_results" not in turns[0]:
            return _object_answer("No verified earlier mapping is available")
        candidates = turns[0]["quoted_tool_results"][0]["items"]
        assert candidates == gateway.candidates
        return _object_call(candidates[1]["reference"])

    result = _history_turn(
        runtime,
        conversation,
        gateway,
        "Try that again",
        [choose_second, _object_answer("Selected room available")],
        seen,
    )
    assert _quoted_turns(seen[retry_start])[0].get("quoted_tool_results") == [
        {"status": "candidates", "items": gateway.candidates}
    ]
    assert result.display_ledger[-1].visible_state == "completed"
    assert len(gateway.calls) == 3
    assert gateway.calls[-1] == {"reference": "object-b"}

    def choose_first(messages):
        candidates = _quoted_turns(messages)[0]["quoted_tool_results"][0]["items"]
        return _object_call(candidates[0]["reference"])

    _history_turn(
        runtime,
        conversation,
        gateway,
        "Use the first room instead",
        [choose_first, _object_answer()],
        seen,
    )
    assert gateway.calls[-1] == {"reference": "object-a"}


@pytest.mark.parametrize("change", ["order", "name", "version", "permission", "time"])
def test_failed_selection_recovery_drops_changed_evidence_before_model(change):
    runtime, conversation, gateway, seen = _failed_selection()
    if change == "order":
        gateway.candidates.reverse()
    elif change == "name":
        gateway.candidates[1]["name"] = "Renamed room"
    elif change == "version":
        gateway.version += 1
    elif change == "permission":
        gateway.proof_current = False
    else:
        gateway.time += 1
    _history_turn(runtime, conversation, gateway, "Try again", [_object_answer()], seen)
    outgoing = str(seen[-1])
    assert "object-b" not in outgoing and "South room" not in outgoing
    assert "The second one" in outgoing and "history_context_unavailable" in outgoing


@pytest.mark.parametrize("change", ["revoked", "stopped"])
def test_failed_selection_recovery_rechecks_after_model_wait(change):
    runtime, conversation, gateway, seen = _failed_selection()

    def invalidate(messages):
        assert _quoted_turns(messages)[0]["quoted_tool_results"][0]["items"] == gateway.candidates
        if change == "revoked":
            gateway.proof_current = False
        else:
            active = runtime.snapshot(conversation, "owner").active_run
            assert active is not None
            runtime.stop_run(conversation, "owner", active.run_ref, active.generation, uuid4())
        return _object_answer("MUST_NOT_PUBLISH")

    with pytest.raises(AssistantRuntimeError):
        _history_turn(runtime, conversation, gateway, "Try again", [invalidate], seen)
    assert runtime.snapshot(conversation, "owner").display_ledger[-1].visible_state != "completed"


@pytest.mark.parametrize("boundary", ["stop", "contract", "identity", "new_conversation", "ttl"])
def test_failed_selection_recovery_respects_runtime_and_contract_boundaries(boundary):
    now = [datetime(2026, 9, 15, tzinfo=UTC)]
    runtime = AssistantRuntime(clock=lambda: now[0], retention_seconds=86400)
    runtime, conversation, gateway, seen = _failed_selection(runtime=runtime)
    if boundary == "identity":
        with pytest.raises(AssistantRuntimeError):
            runtime.accept_turn(conversation, "other-owner", uuid4(), "Try again")
        gateway.live = False
        before = len(seen)
        with pytest.raises(AssistantRuntimeError):
            _history_turn(runtime, conversation, gateway, "Try again", [_object_answer()], seen)
        assert len(seen) == before
        return
    if boundary == "ttl":
        now[0] += timedelta(hours=24)
        with pytest.raises(AssistantRuntimeError):
            runtime.accept_turn(conversation, "owner", uuid4(), "Try again")
        return
    if boundary == "stop":
        stopped = runtime.accept_turn(conversation, "owner", uuid4(), "Stop this")
        runtime.stop_run(conversation, "owner", stopped.run_ref, stopped.generation, uuid4())
    elif boundary == "new_conversation":
        conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    else:
        original = gateway.descriptors
        gateway.descriptors = (
            *original,
            ToolDescriptor(
                name="other.read",
                owner="other",
                kind="read",
                description="Other objects",
                parameters={"type": "object", "properties": {}},
            ),
        )
        gateway.failure = "source_unavailable"
        _history_turn(
            runtime,
            conversation,
            gateway,
            "Try with changed contract",
            [_object_call("object-b"), _object_answer("UNVERIFIED_FAILURE_ANSWER")],
            seen,
        )
        gateway.descriptors, gateway.failure = original, None
    _history_turn(runtime, conversation, gateway, "Try again", [_object_answer()], seen)
    assert "object-b" not in str(seen[-1]) and "South room" not in str(seen[-1])
    assert "past_tool_executions" not in str(seen[-1])


def test_repeated_failed_turns_do_not_extend_recovery_turn_capacity():
    runtime, conversation, gateway, seen = _failed_selection()
    gateway.failure = "source_unavailable"
    for _ in range(7):
        _history_turn(
            runtime,
            conversation,
            gateway,
            "Try again",
            [_object_call("object-b"), _object_answer("UNVERIFIED_FAILURE_ANSWER")],
            seen,
        )
    gateway.failure = None
    _history_turn(runtime, conversation, gateway, "What was selected?", [_object_answer()], seen)
    assert "object-b" not in str(seen[-1]) and "South room" not in str(seen[-1])


@pytest.mark.parametrize("content_proof", [True, False])
def test_native_restore_uses_complete_content_proof_before_full_refresh(content_proof):
    gateway, seen, refreshes = CandidateGateway(), [], []

    def refresh():
        refreshes.append(1)
        return True

    proof = HistoryCheck(
        refresh, revalidate=lambda: True, revalidation_covers_content=content_proof
    )
    gateway.execute = lambda *args: ToolExecutionResult(
        model_content={"status": "available", "fact": "VERIFIED_OBJECT"}, history_check=proof
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    _history_turn(
        runtime, conversation, gateway, "Read object", [_object_call(), _object_answer()], seen
    )
    _history_turn(runtime, conversation, gateway, "Its source?", [_object_answer()], seen)
    assert "VERIFIED_OBJECT" in str(seen[-1])
    assert len(refreshes) == (0 if content_proof else 1)


def test_native_failed_content_proof_refreshes_outside_shared_validation_scope():
    from contextlib import contextmanager

    active, current, refreshes = [False], [True], []

    @contextmanager
    def scope():
        active[0] = True
        try:
            yield
        finally:
            active[0] = False

    def refresh():
        assert not active[0]
        refreshes.append(1)
        current[0] = True
        return True

    proof = HistoryCheck(refresh, revalidate=lambda: current[0], revalidation_covers_content=True)
    gateway, seen = CandidateGateway(), []
    gateway.validation_scope = scope
    gateway.execute = lambda *args: ToolExecutionResult(
        model_content={"fact": "EQUIVALENT_REFRESHED_OBJECT"}, history_check=proof
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    _history_turn(
        runtime, conversation, gateway, "Read object", [_object_call(), _object_answer()], seen
    )
    current[0] = False
    _history_turn(runtime, conversation, gateway, "Its source?", [_object_answer()], seen)
    assert "EQUIVALENT_REFRESHED_OBJECT" in str(seen[-1])
    assert refreshes == [1]


@pytest.mark.parametrize("overflow", [False, True])
def test_schema_rejection_then_authorized_result_keeps_followup_evidence(overflow):
    from tests.school_facts.test_f4_education_gateway import fixture

    _, owner, _, _ = fixture()
    owner.descriptors = tuple(d for d in owner.descriptors if d.name.endswith(".query"))
    gateway, seen = owner.for_turn("owner"), []
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    result = _history_turn(
        runtime,
        conversation,
        gateway,
        "Read the selected class information",
        [
            ModelResponse(parts=[ToolCallPart("tool_0", {"fields": ["unknown-field"]}, "bad")]),
            ModelResponse(
                parts=[ToolCallPart("tool_0", {"name": "2023班", "fields": ["teachers"]}, "good")]
            ),
            _object_answer("Authorized result", overflow=overflow),
        ],
        seen,
    )
    assert result.model_safe_ledger[-1].model_history_checks is not None
    _history_turn(runtime, conversation, gateway, "What was the source?", [_object_answer()], seen)
    outgoing = str(seen[-1])
    assert "陈老师" in outgoing
    assert "history_context_unavailable：部分历史助手或工具输出未提供" not in outgoing
    if overflow:
        assert "quoted_tool_results" in _quoted_turns(seen[-1])[0]
