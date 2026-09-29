from typing import Any, cast

import pytest

from app.composition.school_fact_directory_gateway import (
    DirectoryQueryPort,
    ProductActor,
    directory_model_projection,
)
from app.platform.assistant.contracts import JsonObject
from app.platform.assistant.tool_loop import ToolModel


@pytest.fixture
def maintenance_access_case():
    from types import SimpleNamespace
    from uuid import UUID

    from app.composition.school_fact_f2_query_repository import F2FactQueryRepository
    from app.platform.authorization.secure_reader_contracts import SecureReadDenied
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from app.platform.school_facts.f2_policy import F2FactScope
    from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable
    from tests.test_f2_directory_product import actor

    state = {"mode": "allowed", "purposes": [], "validated": 0}

    class Guard:
        def require_current(self, actual_actor, kind, *, purpose="maintain"):
            assert actual_actor == actor()
            state["purposes"].append(purpose)
            if state["mode"] == "denied":
                raise SchoolFactChangeRejected("private rejection")
            if state["mode"] == "unavailable":
                raise SchoolFactAuthorizationUnavailable("private backend failure")
            return SimpleNamespace(
                scope_plan=SimpleNamespace(
                    reference=F2FactScope(
                        UUID(str(actual_actor.school_id)), kind, ("permitted-one",)
                    ).reference
                ),
                projection=SimpleNamespace(name="test"),
            )

        def revalidate(self, auth, actual_actor):
            assert actual_actor == actor()
            state["validated"] += 1
            if state["mode"] == "revoked_on_release":
                raise SecureReadDenied("revoked during check")

    class Reader:
        def read(self, request):
            raise AssertionError("Maintenance eligibility must not read business records")

    repo = F2FactQueryRepository(
        reader=Reader(), reference_key=b"x" * 32, maintenance_guard=Guard()
    )
    return repo, state


def test_maintenance_access_is_not_empty_creation_options(maintenance_access_case):
    from tests.test_f2_directory_product import actor

    repo, state = maintenance_access_case
    assert repo.creation_choices(actor=actor(), fact_kind="school") == ()
    result = repo.access(actor=actor(), fact_kind="school", purpose="maintain")
    row = result["families"][0]
    assert result["purpose"] == "maintain"
    assert row["status"] == "authorized_scope"
    assert row["authorized_target_count"] == 1
    assert row["authorized_creation_count"] == 0
    assert row["next_step"] == "select_target_and_prepare"
    assert state["validated"] == 1
    assert set(state["purposes"]) == {"maintain"}
    assert "permitted-one" not in str(result)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("denied", "denied"),
        ("unavailable", "source_unavailable"),
        ("revoked_on_release", "source_unavailable"),
    ],
)
def test_maintenance_access_distinguishes_deny_outage_and_release_change(
    maintenance_access_case, mode, expected
):
    from tests.test_f2_directory_product import actor

    repo, state = maintenance_access_case
    state["mode"] = mode
    result = repo.access(actor=actor(), fact_kind="school", purpose="maintain")
    assert result["families"][0] == {"fact_kind": "school", "status": expected}
    assert "private" not in str(result)
    assert "permitted-one" not in str(result)


def test_maintenance_access_target_and_fields_do_not_claim_preflight_success(
    maintenance_access_case,
):
    from tests.test_f2_directory_product import actor

    repo, state = maintenance_access_case
    target = repo.reference_for(actor=actor(), fact_kind="school", source_key="permitted-one")
    result = repo.access(
        actor=actor(),
        fact_kind="school",
        purpose="maintain",
        target_ref=target,
        fields=("display_name",),
    )
    row = result["families"][0]
    assert row["status"] == "requires_prepare"
    assert row["target_in_scope"] is True
    assert row["field_check"] == "schema_only"
    assert row["next_step"] == "prepare"
    assert "allowed_for_request" not in str(result)
    assert state["validated"] == 1


def test_maintenance_access_denies_ungranted_reference_without_record_read(maintenance_access_case):
    from tests.test_f2_directory_product import actor

    repo, _ = maintenance_access_case
    target = repo.reference_for(actor=actor(), fact_kind="school", source_key="other-object")
    result = repo.access(actor=actor(), fact_kind="school", purpose="maintain", target_ref=target)
    assert result["families"][0] == {"fact_kind": "school", "status": "denied"}


@pytest.mark.parametrize(
    "arguments",
    [
        {"purpose": "maintain"},
        {"purpose": "read", "fact_kind": "school", "target_ref": "f2:school:opaque"},
        {"purpose": "maintain", "fact_kind": "school", "fields": ("unknown_field",)},
        {"purpose": "maintain", "fact_kind": "school", "fields": ("location_ref",)},
    ],
)
def test_maintenance_access_rejects_invalid_requests_before_authority(
    maintenance_access_case, arguments
):
    from tests.test_f2_directory_product import actor

    repo, state = maintenance_access_case
    with pytest.raises(ValueError):
        repo.access(actor=actor(), **arguments)
    assert state["purposes"] == []


def test_maintenance_access_relation_field_mapping_uses_owner_contract(maintenance_access_case):
    from tests.test_f2_directory_product import actor

    repo, _ = maintenance_access_case
    target = repo.reference_for(
        actor=actor(), fact_kind="person_workplace", source_key="permitted-one"
    )
    result = repo.access(
        actor=actor(),
        fact_kind="person_workplace",
        purpose="maintain",
        target_ref=target,
        fields=("location_ref",),
    )
    assert result["families"][0]["status"] == "requires_prepare"


def test_maintenance_access_turn_preserves_exact_selection_and_current_proof(
    maintenance_access_case,
):
    from types import SimpleNamespace
    from uuid import uuid4

    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_directory_gateway import MODULE, DirectoryFactToolGateway
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from tests.test_f2_directory_product import actor, setup

    repo, state = maintenance_access_case
    service, boundary, _, _ = setup()
    query = DirectoryProductAdapter(repository=repo)
    target = repo.reference_for(actor=actor(), fact_kind="school", source_key="permitted-one")
    # Reference preparation may inspect coverage; measure the maintenance turn independently.
    state["purposes"].clear()
    arguments = {
        "purpose": "maintain",
        "fact_kind": "school",
        "target_ref": target,
        "fields": ["display_name"],
    }
    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: cast(
            ProductActor, SimpleNamespace(actor=actor(), session_binding="session-one")
        ),
        query=query,
        product=service,
        retention_seconds=60,
    )
    turn = gateway.for_turn("actor")
    result = turn.execute(
        ModelToolCall(call_id="maintenance-check", name=MODULE + ".access", arguments=arguments),
        ToolCallContext("actor", "c", "t", "r", 1, uuid4()),
    )
    assert result.model_content["families"][0]["status"] == "requires_prepare"
    assert result.history_check is not None and result.history_check.valid()
    assert set(state["purposes"]) == {"maintain"}
    assert boundary.calls == []
    state["mode"] = "denied"
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("actor")


@pytest.mark.parametrize("available", [True, False])
def test_maintenance_access_real_guard_separates_missing_binding_from_dependency_failure(available):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from uuid import UUID

    from app.composition.school_fact_f2_query_repository import (
        F2FactQueryRepository,
        F2FinalAuthorizationGuard,
    )
    from app.platform.authorization.thin_contracts import ThinDecisionKind
    from tests.test_f2_directory_product import actor

    class Thin:
        def decide(self, request):
            # The real F2 composition uses this zero UUID for an unbound operation.
            assert request.scope_id == UUID(int=0)
            assert request.action_code.endswith("maintain")
            if not available:
                raise RuntimeError("private dependency detail")
            return SimpleNamespace(kind=ThinDecisionKind.DENY, basis=None)

    guard = F2FinalAuthorizationGuard(
        thin_service=Thin(),
        scope_id_resolver=lambda operation, identity: UUID(int=0),
        clock=lambda: datetime(2026, 9, 22, tzinfo=UTC),
    )
    repo = F2FactQueryRepository(
        reader=SimpleNamespace(), reference_key=b"x" * 32, maintenance_guard=guard
    )
    result = repo.access(actor=actor(), fact_kind="school", purpose="maintain")
    assert result["families"][0]["status"] == ("denied" if available else "source_unavailable")
    assert "private" not in str(result)


def test_policy_excerpt_is_explicit_and_full_input_unchanged():
    original = "甲乙丙丁" * 2000
    data = {
        "status": "known",
        "records": [
            {
                "reference": "f2:policy_version:abcdefgh",
                "fields": {"content_text": original},
                "version": 2,
            }
        ],
        "matched_count": 1,
        "truncated": False,
    }
    result = cast(Any, directory_model_projection(data))
    fields = result["records"][0]["fields"]
    assert "content_text" not in fields
    assert fields["original_text"]["length"] == len(original)
    assert fields["original_text"]["complete"] is False
    assert fields["original_text"]["excerpt"] == original[:1200]
    assert data["records"][0]["fields"]["content_text"] == original


def test_projection_never_calls_shown_subset_the_total():
    data = {
        "status": "known",
        "records": [
            {"reference": f"f2:person:abcdefgh{i}", "fields": {"display_name": str(i)}}
            for i in range(20)
        ],
        "matched_count": 20,
        "truncated": False,
    }
    result = cast(Any, directory_model_projection(data))
    assert (
        len(result["records"]) == 20 and result["matched_count"] == 20 and not result["truncated"]
    )
    assert result["returned_count"] == len(result["records"])


def test_model_prepare_does_not_offer_confirmation_before_full_workbench_review():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE, DirectoryFactToolGateway
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from tests.test_f2_directory_product import actor, setup

    service, boundary, _, payload = setup()
    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: cast(
            ProductActor, SimpleNamespace(actor=actor(), session_binding="session-one")
        ),
        query=cast(DirectoryQueryPort, SimpleNamespace()),
        product=service,
        retention_seconds=60,
    )
    result = gateway.execute(
        ModelToolCall(
            call_id="prepare", name=MODULE + ".prepare", arguments=payload.model_dump(mode="json")
        ),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    assert result.module_result is not None
    assert result.module_result.action_offer is None
    assert result.model_content["outcome"] == "候选已准备，尚未正式执行"
    assert isinstance(result.model_content["tool_context"]["action_number"], int)
    assert result.fact_card["proposal_ref"] not in str(result.model_content)
    assert not any(name == "confirm" for name, _ in boundary.calls)


def test_quality_grant_revocation_invalidates_already_read_tool_result():
    from types import SimpleNamespace
    from uuid import uuid4

    import pytest

    from app.composition.school_fact_directory_gateway import MODULE, DirectoryFactToolGateway
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from tests.test_f2_directory_product import actor, setup

    service, _, _, _ = setup()
    live = [True]

    class Query:
        def query(self, **kwargs):
            return {
                "status": "known",
                "records": [],
                "quality": {"evidence_status": "known" if live[0] else "forbidden"},
            }

        def require_query(self, **kwargs):
            pass

    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: cast(
            ProductActor, SimpleNamespace(actor=actor(), session_binding="session-one")
        ),
        query=cast(DirectoryQueryPort, Query()),
        product=service,
        retention_seconds=60,
    )
    turn = gateway.for_turn("private-actor")
    turn.execute(
        ModelToolCall(call_id="read", name=MODULE + ".query", arguments={"fact_kind": "school"}),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    live[0] = False
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("private-actor")


def test_read_denial_and_authority_outage_are_distinct_tool_states():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE, DirectoryFactToolGateway
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from app.platform.authorization.secure_reader_contracts import SecureReadDenied
    from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable
    from tests.test_f2_directory_product import actor, setup

    service, _, _, _ = setup()
    for error, status in (
        (SecureReadDenied, "denied"),
        (SchoolFactAuthorizationUnavailable, "source_unavailable"),
    ):

        class Query:
            def __init__(self, exception_type):
                self.exception_type = exception_type

            def query(self, **kwargs):
                raise self.exception_type("private authority diagnostic")

        gateway = DirectoryFactToolGateway(
            resolve_actor=lambda _: cast(
                ProductActor, SimpleNamespace(actor=actor(), session_binding="session-one")
            ),
            query=cast(DirectoryQueryPort, Query(error)),
            product=service,
            retention_seconds=60,
        )
        result = gateway.execute(
            ModelToolCall(
                call_id="read", name=MODULE + ".query", arguments={"fact_kind": "organization"}
            ),
            ToolCallContext("private", "dialogue", "turn", "run", 1, uuid4()),
        )
        assert result.model_content["status"] == status
        assert "private authority diagnostic" not in str(result.model_content)
        assert result.history_check is None
        assert result.history_execution_note == (
            "read_returned_no_values" if status == "denied" else None
        )


def _directory_read_turn(query=None):
    from types import SimpleNamespace

    from app.composition.school_fact_directory_gateway import DirectoryFactToolGateway
    from tests.test_f2_directory_product import actor, setup

    service, boundary, _, _ = setup()
    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: cast(
            ProductActor, SimpleNamespace(actor=actor(), session_binding="session-one")
        ),
        query=cast(DirectoryQueryPort, query or SimpleNamespace()),
        product=service,
        retention_seconds=60,
    )
    return gateway, gateway.for_turn("private-actor"), boundary


def _assert_release_invalidated(turn, result):
    import pytest

    assert result.history_check is not None
    assert result.history_check.valid() is False
    assert result.history_check.refresh() is False
    from app.platform.assistant.runtime import AssistantRuntimeError

    with pytest.raises((AssistantRuntimeError, PermissionError, ValueError)):
        turn.revalidate("private-actor")


@pytest.mark.parametrize(
    "change",
    [
        "person_ambiguity",
        "person_revoked",
        "assignment_revoked",
        "assignment_ended",
        "field_removed",
        "source_changed",
    ],
)
def test_person_assignment_lookup_revalidates_both_dependencies_before_reuse(change):
    from copy import deepcopy
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    live = {
        "person": {
            "kind": "directory_facts",
            "fact_kind": "person",
            "status": "known",
            "records": [
                {"reference": "person:su", "label": "苏老师", "fields": {"display_name": "苏老师"}}
            ],
            "truncated": False,
            "coverage": {"status": "unknown"},
        },
        "assignment": {
            "kind": "directory_facts",
            "fact_kind": "assignment",
            "status": "known",
            "records": [
                {
                    "reference": "assignment:su",
                    "label": "数学教师",
                    "fields": {"person_ref": "person:su", "position_title": "数学教师"},
                    "source": {"label": "正式发布"},
                }
            ],
            "truncated": False,
            "coverage": {"status": "unknown"},
        },
    }
    calls = []

    class Query:
        def query(self, *, actor: Any, payload):
            from tests.test_f2_directory_product import actor as expected_actor

            assert actor == expected_actor()
            assert payload.scope == "selected"
            assert (
                payload.fact_kind == "person"
                and payload.query_text == "苏老师"
                or payload.fact_kind == "assignment"
                and payload.related_ref == "person:su"
            )
            calls.append(payload.fact_kind)
            return deepcopy(live[payload.fact_kind])

    _, turn, _ = _directory_read_turn(Query())
    result = turn.execute(
        ModelToolCall(
            call_id="person-assignment",
            name=MODULE + ".lookup",
            arguments={"subject_kind": "person", "query_text": "苏老师", "include": ["assignment"]},
        ),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    assert result.fact_card is not None
    assert result.fact_card["results"][1]["records"][0]["reference"] == "assignment:su"
    assert result.history_check is not None and result.history_check.valid()
    assert "assignment:su" in str(result.model_content)
    assert calls == ["person", "assignment", "person", "assignment"]
    if change == "person_ambiguity":
        live["person"]["records"].append(
            {"reference": "person:other", "label": "苏老师", "fields": {"display_name": "苏老师"}}
        )
    elif change.endswith("revoked"):
        kind = change.removesuffix("_revoked")
        live[kind].update(status="denied", records=[])
    elif change == "assignment_ended":
        live["assignment"].update(status="no_current_records", records=[])
    elif change == "field_removed":
        live["assignment"]["records"][0]["fields"].pop("position_title")
    else:
        live["assignment"]["records"][0]["source"]["label"] = "已补正来源"
    _assert_release_invalidated(turn, result)


def test_receipt_release_checks_original_target_even_with_unrelated_maintenance(monkeypatch):
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from app.platform.school_facts.ports import SchoolFactApplyResult
    from tests.test_f2_directory_product import actor

    gateway, turn, boundary = _directory_read_turn()
    boundary.result = SchoolFactApplyResult(uuid4(), 8, 1, False)
    lookup = boundary.check_result_by_locator
    exact_allowed = [True]

    def exact_lookup(**kwargs):
        if not exact_allowed[0]:
            raise PermissionError("original target revoked; unrelated family still allowed")
        return lookup(**kwargs)

    monkeypatch.setattr(boundary, "check_result_by_locator", exact_lookup)
    result = turn.execute(
        ModelToolCall(
            call_id="receipt",
            name=MODULE + ".receipt",
            arguments={"proposal_ref": "sf3plan_" + "a" * 32},
        ),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    turn.revalidate("private-actor")
    assert result.history_check is not None
    assert result.history_check.valid() and result.history_check.refresh()
    exact_allowed[0] = False
    gateway.product.require_receipt_access(actor())
    _assert_release_invalidated(turn, result)
    assert all(name == "receipt" for name, _ in boundary.calls)


def test_receipt_revalidation_keeps_resolved_reference_when_recent_candidate_changes():
    from datetime import UTC, datetime, timedelta
    from hashlib import sha256
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from app.platform.school_facts.ports import SchoolFactApplyResult

    gateway, turn, boundary = _directory_read_turn()
    boundary.result = SchoolFactApplyResult(uuid4(), 8, 1, False)
    reference = "sf3plan_" + "a" * 32
    expiry = datetime.now(UTC) + timedelta(seconds=60)
    gateway._recent[("private-actor", "dialogue")] = (reference, expiry)
    result = turn.execute(
        ModelToolCall(call_id="receipt", name=MODULE + ".receipt", arguments={}),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    gateway._recent[("private-actor", "dialogue")] = ("sf3plan_" + "b" * 32, expiry)
    turn.revalidate("private-actor")
    assert result.history_check is not None
    assert result.history_check.valid() and result.history_check.refresh()
    assert len(boundary.calls) >= 4
    assert {kwargs["locator_sha256"] for _, kwargs in boundary.calls} == {
        sha256(reference.encode()).hexdigest()
    }


@pytest.mark.parametrize(
    "change",
    [
        {"text": "updated original"},
        {"version": 3},
        {"source": {"label": "updated source"}},
        {"length": 8, "has_more": False},
        {"status": "no_current_records"},
        {"status": "forbidden"},
    ],
)
def test_original_release_rechecks_body_version_source_and_availability(change):
    from copy import deepcopy
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    reference = "f2:policy_version:abcdefgh"
    baseline = {
        "kind": "policy_original",
        "reference": reference,
        "version": 2,
        "text": "正式原文甲",
        "offset": 3,
        "end": 8,
        "length": 10,
        "complete": False,
        "has_more": True,
        "source": {"label": "正式来源甲", "kind": "publication", "reference": "publication:1"},
    }
    live = deepcopy(baseline)
    requests = []

    class Query:
        def original(self, **kwargs):
            requests.append(kwargs)
            if live.get("status") == "no_current_records":
                raise ValueError("Policy version unavailable")
            if live.get("status") == "forbidden":
                raise PermissionError("Policy body denied")
            return deepcopy(live)

        def require_query(self, **kwargs):
            pass

    _, turn, _ = _directory_read_turn(Query())
    result = turn.execute(
        ModelToolCall(
            call_id="original",
            name=MODULE + ".original",
            arguments={"reference": reference, "offset": 3, "limit": 5},
        ),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    turn.revalidate("private-actor")
    assert result.history_check is not None
    assert result.history_check.valid() and result.history_check.refresh()
    live.update(change)
    _assert_release_invalidated(turn, result)
    assert all(item["reference"] == reference for item in requests)
    assert all(item["offset"] == 3 and item["limit"] == 5 for item in requests)
    assert all(item["as_of"] is None for item in requests)


@pytest.mark.parametrize("action", ["query", "original", "prepare"])
def test_tool_json_dates_preserve_explicit_fact_time_at_release(action):
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from tests.test_f2_directory_product import NOW, setup

    times = []

    class Query:
        def query(self, *, actor, payload):
            times.append(payload.as_of)
            return {"kind": "directory_facts", "status": "known", "records": []}

        def original(self, *, actor, reference, offset, limit, as_of=None):
            times.append(as_of)
            return {"kind": "policy_original", "reference": reference, "text": "original"}

    gateway, turn, boundary = _directory_read_turn(Query())
    _, _, _, payload = setup()
    if action == "prepare":
        arguments = payload.model_dump(mode="json")
        arguments["effective_at"] = NOW.isoformat()
    elif action == "query":
        arguments = {"fact_kind": "school", "as_of": NOW.isoformat()}
    else:
        arguments = {"reference": "f2:policy_version:abcdefgh", "as_of": NOW.isoformat()}
    result = turn.execute(
        ModelToolCall(
            call_id=action, name=MODULE + "." + action, arguments=cast(JsonObject, arguments)
        ),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    assert result.model_content.get("status") != "rejected"
    turn.revalidate("private-actor")
    assert result.history_check is not None
    assert result.history_check.valid() and result.history_check.refresh()
    if action == "prepare":
        assert boundary.calls[0][1]["change_set"].changes[0].effective_at == NOW
        assert all(name != "confirm" for name, _ in boundary.calls)
    else:
        assert len(times) >= 4 and all(value == NOW for value in times)


@pytest.mark.parametrize(
    "arguments",
    [
        {"reference": "f2:policy_version:abcdefgh", "offset": "1"},
        {"reference": "f2:policy_version:abcdefgh", "as_of": "2026-09-06T00:00:00"},
        {"reference": "f2:policy_version:abcdefgh", "as_of": 1788652800},
        {"reference": "f2:policy_version:abcdefgh", "school_id": "untrusted"},
    ],
)
def test_tool_json_dates_keep_strict_integer_timezone_and_identity_contract(arguments):
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    _, turn, boundary = _directory_read_turn()
    result = turn.execute(
        ModelToolCall(
            call_id="original", name=MODULE + ".original", arguments=cast(JsonObject, arguments)
        ),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    assert result.model_content["status"] == "rejected"
    assert result.history_check is None and boundary.calls == []


@pytest.mark.parametrize("action", ["receipt", "original"])
@pytest.mark.parametrize("stage", ["before_next_model", "before_settlement"])
def test_actual_tool_loop_blocks_changed_result_at_release_checkpoints(monkeypatch, action, stage):
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE
    from app.platform.assistant.runtime import AssistantRuntime, AssistantRuntimeError
    from app.platform.assistant.tool_loop import AssistantToolLoop, ModelReply, ModelToolCall
    from app.platform.school_facts.ports import SchoolFactApplyResult
    from tests.assistant.test_tool_loop import ScriptModel

    live = [True]

    class Query:
        def original(self, **kwargs):
            return {"kind": "policy_original", "text": "original" if live[0] else "updated"}

    _, turn, boundary = _directory_read_turn(Query())
    boundary.result = SchoolFactApplyResult(uuid4(), 8, 1, False)
    lookup = boundary.check_result_by_locator

    def exact_lookup(**kwargs):
        if not live[0]:
            raise PermissionError("original target revoked")
        return lookup(**kwargs)

    monkeypatch.setattr(boundary, "check_result_by_locator", exact_lookup)
    execute = turn.execute

    def read_then_change(call, context):
        result = execute(call, context)
        if stage == "before_next_model":
            live[0] = False
        return result

    monkeypatch.setattr(turn, "execute", read_then_change)
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("private-actor", uuid4())
    accepted = runtime.accept_turn(
        conversation.conversation_ref, "private-actor", uuid4(), "Read the original result"
    )
    settlements = []
    settle = runtime.settle_run

    def record_settlement(*args, **kwargs):
        settlements.append(True)
        return settle(*args, **kwargs)

    monkeypatch.setattr(runtime, "settle_run", record_settlement)

    def change_then_answer(request):
        live[0] = False
        return ModelReply(text="This result must not settle")

    arguments = (
        {"proposal_ref": "sf3plan_" + "a" * 32}
        if action == "receipt"
        else {"reference": "f2:policy_version:abcdefgh"}
    )
    model = ScriptModel(
        [
            ModelReply(
                calls=(
                    ModelToolCall(
                        call_id=action,
                        name=MODULE + "." + action,
                        arguments=cast(JsonObject, arguments),
                    ),
                )
            ),
            change_then_answer,
        ]
    )
    with pytest.raises((AssistantRuntimeError, PermissionError)):
        AssistantToolLoop(
            model=cast(ToolModel, model), gateway_factory=lambda _: turn
        ).run_accepted(
            runtime=runtime,
            accepted=accepted,
            actor_context_ref="private-actor",
            request_id=uuid4(),
        )
    assert len(model.requests) == (1 if stage == "before_next_model" else 2)
    assert settlements == []
    assert all(name == "receipt" for name, _ in boundary.calls)


def test_each_history_check_revalidates_only_its_own_result():
    from collections import Counter
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    reads = Counter()
    live = {"school": True, "organization": True, "location": True}

    class Query:
        def query(self, *, actor, payload):
            reads[payload.fact_kind] += 1
            return {
                "kind": "directory_facts",
                "status": "known" if live[payload.fact_kind] else "denied",
                "records": [],
            }

    _, turn, _ = _directory_read_turn(Query())
    checks = []
    for kind in live:
        result = turn.execute(
            ModelToolCall(call_id=kind, name=MODULE + ".query", arguments={"fact_kind": kind}),
            ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
        )
        assert result.history_check is not None
        checks.append(result.history_check)
    reads.clear()
    assert all(check.valid() for check in checks)
    assert reads == Counter({kind: 1 for kind in live})
    reads.clear()
    assert all(check.refresh() for check in checks)
    assert reads == Counter({kind: 1 for kind in live})
    live["location"] = False
    assert checks[0].valid() and checks[1].valid()
    assert checks[2].valid() is False
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("private-actor")


def test_unsupported_selector_keeps_safe_dialogue_history_without_fact_reads():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE, DirectoryFactToolGateway
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from tests.test_f2_directory_product import actor, real_directory_adapter, setup

    service, _, _, _ = setup()
    adapter, _ = real_directory_adapter("organization_campus_relationship", {})
    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: cast(
            ProductActor, SimpleNamespace(actor=actor(), session_binding="session-one")
        ),
        query=adapter,
        product=service,
        retention_seconds=60,
    )
    turn = gateway.for_turn("private-actor")
    result = turn.execute(
        ModelToolCall(
            call_id="unsupported",
            name=MODULE + ".query",
            arguments={"fact_kind": "organization_campus_relationship", "query_text": "组织名字"},
        ),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    assert result.model_content["status"] == "unsupported_selection"
    assert result.fact_card["status"] == result.model_content["status"]
    assert result.fact_card["query_support"] == result.model_content["query_support"]
    assert result.history_check is not None and result.history_check.valid()
    turn.revalidate("private-actor")


def test_access_scope_is_revalidated_without_querying_business_records():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE, DirectoryFactToolGateway
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from tests.test_f2_directory_product import actor, setup

    live = [True]

    class Query:
        def access(self, *, actor, fact_kind=None):
            return {
                "kind": "fact_access",
                "requested_fact_kind": fact_kind,
                "status": "known",
                "families": [
                    {"fact_kind": "location", "status": "authorized_scope" if live[0] else "denied"}
                ],
            }

        def query(self, **kwargs):
            raise AssertionError("Access explanation must not read business data")

    service, _, _, _ = setup()
    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: cast(
            ProductActor, SimpleNamespace(actor=actor(), session_binding="session-one")
        ),
        query=cast(DirectoryQueryPort, Query()),
        product=service,
        retention_seconds=60,
    )
    turn = gateway.for_turn("actor")
    result = turn.execute(
        ModelToolCall(call_id="a", name=MODULE + ".access", arguments={}),
        ToolCallContext("actor", "c", "t", "r", 1, uuid4()),
    )
    assert result.model_content["kind"] == "fact_access"
    assert result.model_content["requested_fact_kind"] is None
    assert result.fact_card["requested_fact_kind"] is None
    assert result.history_check is not None
    live[0] = False
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("actor")
    assert not result.history_check.valid()


def test_access_repository_uses_authority_not_fact_reader_and_separates_denied():
    from types import SimpleNamespace
    from uuid import UUID

    from app.composition.school_fact_f2_query_repository import F2FactQueryRepository
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from app.platform.school_facts.f2_policy import F2FactScope
    from tests.test_f2_directory_product import actor

    class Guard:
        def require_current(self, actor, kind, *, purpose):
            assert purpose == "read"
            if kind == "policy":
                raise SchoolFactChangeRejected("denied")
            return SimpleNamespace(
                scope_plan=SimpleNamespace(
                    reference=F2FactScope(
                        UUID(str(actor.school_id)), kind, ("alpha", "beta")
                    ).reference
                ),
                projection=SimpleNamespace(name="test"),
            )

        def revalidate(self, auth, actor):
            pass

        def can_read_self(self, actor):
            return False

    class Reader:
        def read(self, request):
            raise AssertionError("must not fetch records")

    repo = F2FactQueryRepository(
        reader=Reader(), reference_key=b"x" * 32, maintenance_guard=Guard()
    )
    assert (
        repo.access(actor=actor(), fact_kind="location")["families"][0]["authorized_target_count"]
        == 2
    )
    selected = repo.access(actor=actor(), fact_kind="policy")
    assert selected["families"][0]["status"] == "denied"
    assert selected["requested_fact_kind"] == "policy"
    overview = repo.access(actor=actor())
    assert overview["requested_fact_kind"] is None
    assert len(overview["families"]) > 1


def test_access_http_is_bound_to_authenticated_identity_and_no_store():
    from types import SimpleNamespace

    from tests.test_f2_directory_http import client_for
    from tests.test_f2_directory_product import actor, setup

    calls = []

    def access(**kwargs):
        calls.append(kwargs)
        return {"kind": "fact_access", "requested_fact_kind": kwargs["fact_kind"], "families": []}

    service, _, _, _ = setup()
    client, _, _ = client_for(service, SimpleNamespace(access=access))
    response = client.get("/api/v1/school-fact-product/facts/access?fact_kind=policy")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["requested_fact_kind"] == "policy"
    assert calls[0]["actor"] == actor()
    assert calls[0]["fact_kind"] == "policy"
    assert (
        client.get("/api/v1/school-fact-product/facts/access?fact_kind=not_installed").status_code
        == 422
    )


def test_access_revocation_during_revalidation_releases_no_stale_scope_metadata():
    from types import SimpleNamespace
    from uuid import UUID

    from app.composition.school_fact_f2_query_repository import F2FactQueryRepository
    from app.platform.authorization.secure_reader_contracts import SecureReadDenied
    from app.platform.school_facts.f2_policy import F2FactScope
    from tests.test_f2_directory_product import actor

    class Guard:
        def require_current(self, actor, kind, *, purpose):
            return SimpleNamespace(
                scope_plan=SimpleNamespace(
                    reference=F2FactScope(
                        UUID(str(actor.school_id)), kind, ("secret-target",)
                    ).reference
                ),
                projection=SimpleNamespace(name="test"),
            )

        def revalidate(self, auth, actor):
            raise SecureReadDenied("revoked")

    repo = F2FactQueryRepository(
        reader=SimpleNamespace(), reference_key=b"x" * 32, maintenance_guard=Guard()
    )
    item = repo.access(actor=actor(), fact_kind="policy")["families"][0]
    assert item == {"fact_kind": "policy", "status": "source_unavailable"}


def test_model_material_omits_engineering_envelope_without_hiding_quality_risk():
    from app.composition.school_fact_directory_gateway import directory_model_material

    view = {
        "kind": "directory_facts",
        "status": "known",
        "query_support": {"guidance": "technical"},
        "query": {"reference": "private-routing"},
        "records": [
            {
                "reference": "f2:location:abcdefgh",
                "label": "某校区",
                "version": 2,
                "fields": {"display_name": "某校区"},
                "source": {
                    "label": "正式发布",
                    "reference": "publication:internal",
                    "published_at": "today",
                },
            }
        ],
        "quality": {
            "source_availability": "unknown",
            "conflict": "open",
            "evidence_status": "known",
            "notice": "repeated technical explanation",
            "statements": [],
        },
        "coverage": {"status": "partial", "description": "仅本对象"},
        "returned_count": 1,
    }
    material = directory_model_material(view)
    assert "query_support" not in material and "query" not in material
    assert material["readable_quality"]["conflict"] == "正式资料存在待核实争议"
    assert (
        material["readable_quality"]["source_statement"]
        == "未取得适用的来源可用性声明，原来源状态尚不能确定"
    )
    assert material["records"][0]["reference"] == "f2:location:abcdefgh"
    assert material["records"][0]["source"] == {"label": "正式发布", "published_at": "today"}
    assert material["reality_coverage"] == {"meaning": "部分覆盖"}
    assert material["authorized_result_set"]["scope"] == view["coverage"]["description"]
    assert view["records"][0]["source"]["reference"] == "publication:internal"


def test_unsupported_query_keeps_usable_support_in_model_material():
    from app.composition.school_fact_directory_gateway import directory_model_material

    view = {
        "status": "unsupported_selection",
        "query_support": {"guidance": "使用已获权引用"},
        "records": [],
    }
    assert directory_model_material(view)["query_support"] == view["query_support"]


def test_composite_denied_component_still_revalidates_authorized_evidence(monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    import app.composition.school_fact_directory_gateway as module
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from tests.test_f2_directory_product import actor, setup

    live = [True]

    def lookup(*args, **kwargs):
        return {
            "kind": "directory_bundle",
            "status": "denied",
            "subject_label": "组织",
            "results": [
                {"status": "known", "records": [{"reference": "a", "version": 1 if live[0] else 2}]}
            ],
        }

    monkeypatch.setattr(module, "lookup", lookup)
    service, _, _, _ = setup()
    gateway = module.DirectoryFactToolGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=actor(), session_binding="session"),
        query=SimpleNamespace(),
        product=service,
        retention_seconds=60,
    )
    turn = gateway.for_turn("actor")
    result = turn.execute(
        ModelToolCall(
            call_id="a",
            name=module.MODULE + ".lookup",
            arguments={
                "subject_kind": "organization",
                "query_text": "组织",
                "include": ["official_contact"],
            },
        ),
        ToolCallContext("actor", "c", "t", "r", 1, uuid4()),
    )
    assert result.history_check is not None
    live[0] = False
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("actor")


@pytest.mark.parametrize("bundled", [False, True])
@pytest.mark.parametrize("include_supporting_evidence", [False, True])
def test_model_material_separates_provenance_without_losing_document_numbers(
    bundled, include_supporting_evidence
):
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import directory_model_material

    provenance = {
        "basis_reference": "opaque:run-428",
        "approval_reference": "校办〔2026〕17号",
        "consent_reference": "2026年联系资料授权书",
        "evidence_reference": "https://school.example/evidence/42",
    }
    record = {
        "reference": "f2:organization:abcdefgh",
        "fields": {
            "display_name": "教务处",
            "organization_ref": "f2:organization:ijklmnop",
            **provenance,
        },
        "source": {
            "label": "正式发布",
            "reference": "publication:private",
            "published_at": "today",
        },
    }
    claim = {
        "reference": "f2:fact_quality:abcdefgh",
        "owner_ref": record["reference"],
        "applies_to_records": [record["reference"]],
        "applies_to_query": False,
        "dispute_status": "open",
        **provenance,
    }
    query = {
        "status": "known",
        "records": [record],
        "quality": {
            "source_availability": "unknown",
            "evidence_truncated": True,
            "statements": [claim],
        },
        "truncated": True,
        "matched_count": None,
    }
    view = (
        {"kind": "directory_bundle", "subject_reference": record["reference"], "results": [query]}
        if bundled
        else query
    )
    before = deepcopy(view)
    material = directory_model_material(
        view, include_supporting_evidence=include_supporting_evidence
    )
    expected_evidence = (
        provenance if include_supporting_evidence else {"available_fields": list(provenance)}
    )
    result = material["results"][0] if bundled else material
    assert result["records"][0]["supporting_evidence"] == expected_evidence
    assert result["records"][0]["fields"] == {
        "display_name": "教务处",
        "organization_ref": "f2:organization:ijklmnop",
    }
    assert result["records"][0]["reference"] == record["reference"]
    assert result["records"][0]["source"] == {"label": "正式发布", "published_at": "today"}
    statement = result["readable_quality"]["statements"][0]
    operands = statement["tool_context"]
    assert result["readable_quality"]["statements"][0]["supporting_evidence"] == expected_evidence
    assert not set(provenance).intersection(statement)
    assert operands["reference"] == claim["reference"]
    assert operands["owner_ref"] == claim["owner_ref"]
    assert operands["applies_to_records"] == claim["applies_to_records"]
    assert statement["scope"]["application"] == "仅限声明列明的对象与条件"
    assert statement["details"]["争议状态"] == "正式资料存在待核实争议"
    assert (
        result["readable_quality"]["source_statement"]
        == "未取得适用的来源可用性声明，原来源状态尚不能确定"
    )
    assert (
        result["readable_quality"]["reading_limit"]
        == "质量声明未全部返回，不能据此确定完整质量结论"
    )
    assert result["truncated"] is True and result["matched_count"] is None
    if bundled:
        assert material["subject_reference"] == record["reference"]
    if not include_supporting_evidence:
        import json

        encoded = json.dumps(material, ensure_ascii=False)
        assert all(value not in encoded for value in provenance.values())
    assert view == before


def test_directory_answer_guidance_is_exported_without_repeating_in_every_descriptor():
    from app.composition.school_fact_directory_gateway import (
        DIRECTORY_ASSISTANT_INSTRUCTION,
        DirectoryFactToolGateway,
    )

    assert "supporting_evidence" in DIRECTORY_ASSISTANT_INSTRUCTION
    assert "include_supporting_evidence" in DIRECTORY_ASSISTANT_INSTRUCTION
    assert "include_routing_details" in DIRECTORY_ASSISTANT_INSTRUCTION
    assert "已提供发布来源" in DIRECTORY_ASSISTANT_INSTRUCTION
    assert "内部" in DIRECTORY_ASSISTANT_INSTRUCTION
    assert "原文" in DIRECTORY_ASSISTANT_INSTRUCTION
    for tool in DirectoryFactToolGateway.descriptors:
        assert DIRECTORY_ASSISTANT_INSTRUCTION not in tool.description


def test_model_material_preserves_policy_original_and_excerpt_verbatim():
    from app.composition.school_fact_directory_gateway import directory_model_material

    text = "校办〔2026〕17号\n正文含 basis_reference 与 opaque:run-428，逐字保留。"
    view = {
        "kind": "policy_original",
        "reference": "f2:policy_version:abcdefgh",
        "text": text,
        "offset": 0,
        "end": len(text),
        "length": len(text),
        "complete": True,
        "has_more": False,
    }
    assert directory_model_material(view) == view
    query = {"records": [{"fields": {"content_text": text}}]}
    projection = directory_model_projection(query)
    assert (
        directory_model_material(projection)["records"][0]["fields"]["original_text"]["excerpt"]
        == text
    )


def _resolved_bundle_material_fixture():
    def record(reference, fact_kind, **fields):
        return {
            "reference": reference,
            "fact_kind": fact_kind,
            "label": fields.get("display_name", fact_kind),
            "fields": fields,
            "version": 1,
            "source": {
                "kind": "formal_publication",
                "label": "正式发布",
                "reference": "publication:internal",
                "published_at": "2026-09-08",
            },
        }

    def query(fact_kind, records, status="known", **extra):
        return {
            "fact_kind": fact_kind,
            "status": status,
            "records": records,
            "quality": {"source_availability": "unknown", "statements": []},
            "coverage": {"status": "partial"},
            "factual_at": "2026-09-08T08:00:00Z",
            "queried_at": "2026-09-08T08:01:00Z",
            "truncated": False,
            "returned_count": len(records),
            "matched_count": len(records),
            **extra,
        }

    selected = record("f2:organization:selected", "organization", display_name="教务处")
    others = [
        record(
            f"f2:organization:other{i}",
            "organization",
            display_name="教务处",
            business_code="long-other-detail-" * 40,
        )
        for i in range(6)
    ]
    campus = record("f2:location:campus", "location", display_name="南校区", kind="campus")
    relation = record(
        "f2:organization_campus_relationship:selected",
        "organization_campus_relationship",
        organization_ref=selected["reference"],
        campus_ref=campus["reference"],
    )
    other_relation = record(
        "f2:organization_campus_relationship:other",
        "organization_campus_relationship",
        organization_ref=others[0]["reference"],
        campus_ref="f2:location:other-campus",
    )
    contact = record(
        "f2:official_contact:selected",
        "official_contact",
        owner_ref=selected["reference"],
        contact_value="office@example.invalid",
        basis_reference="校办〔2026〕17号",
    )
    return {
        "kind": "directory_bundle",
        "status": "known",
        "subject_label": "教务处",
        "subject_reference": selected["reference"],
        "requested_fact_kinds": ["official_contact", "organization_profile"],
        "results": [
            query("organization", [selected, *others], "ambiguous"),
            query("location", [campus]),
            query("organization_campus_relationship", [relation]),
            query("organization_campus_relationship", [other_relation]),
            query("official_contact", [contact]),
            query("organization_profile", [], "denied"),
        ],
    }


def test_resolved_bundle_material_separates_current_target_from_resolution_checks():
    import json
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import directory_model_material

    view = _resolved_bundle_material_fixture()
    before = deepcopy(view)
    material = directory_model_material(view)
    assert "results" not in material
    assert material["resolved_subject"]["tool_context"]["reference"] == view["subject_reference"]
    assert material["status"] == "known"
    assert [fact["tool_context"]["fact_kind"] for fact in material["facts"]] == view[
        "requested_fact_kinds"
    ]
    assert material["facts"][0]["records"][0]["supporting_evidence"] == {
        "available_fields": ["basis_reference"]
    }
    assert material["facts"][1]["status"] == "denied"
    assert {row["tool_context"]["reference"] for row in material["context"]} == {
        "f2:location:campus",
        "f2:organization_campus_relationship:selected",
    }
    checks = material["resolution_checks"]
    assert len(checks) == 4
    assert checks[0]["status"] == "ambiguous"
    for original, check in zip(view["results"][:4], checks, strict=True):
        assert "records" not in check
        assert check["record_details_in_evidence_only"] is True
        for field in (
            "status",
            "factual_at",
            "queried_at",
        ):
            assert check[field] == original[field]
        assert check["readable_quality"] == {
            "source_statement": "未取得适用的来源可用性声明，原来源状态尚不能确定",
            "statements": [],
        }
        assert check["reality_coverage"] == {"meaning": "部分覆盖"}
        for field in ("truncated", "returned_count", "matched_count"):
            assert check["authorized_result_set"][field] == original[field]
    assert material["resolution_complete"] is True
    assert "long-other-detail" not in json.dumps(material)
    assert len(json.dumps(material)) < len(json.dumps(view)) * 0.7
    assert view == before


@pytest.mark.parametrize("status", ["ambiguous", "partial"])
def test_unresolved_bundle_keeps_authorized_candidates_for_clarification(status):
    from app.composition.school_fact_directory_gateway import directory_model_material

    view = _resolved_bundle_material_fixture()
    view.pop("subject_reference")
    view["status"] = status
    view["candidates"] = view["results"][0]["records"]
    material = directory_model_material(view)
    assert "resolved_subject" not in material
    assert len(material["results"][0]["records"]) == 7
    assert len(material["candidates"]) == 7
    assert material["status"] == status


def test_resolved_bundle_retains_requested_failure_and_resolution_risks():
    from app.composition.school_fact_directory_gateway import directory_model_material

    view = _resolved_bundle_material_fixture()
    view["status"] = "source_unavailable"
    view["results"][1].update(status="source_unavailable", truncated=True)
    view["results"][1]["quality"].update(
        conflict="open", freshness="review_overdue", evidence_truncated=True
    )
    material = directory_model_material(view)
    assert material["status"] == "source_unavailable"
    assert material["facts"][1]["status"] == "denied"
    check = material["resolution_checks"][1]
    assert (
        check["status"] == "source_unavailable"
        and check["authorized_result_set"]["truncated"] is True
    )
    assert check["readable_quality"]["conflict"] == "正式资料存在待核实争议"
    assert check["readable_quality"]["freshness"] == "已到声明规定的核实期限"
    assert "未全部返回" in check["readable_quality"]["reading_limit"]


def test_compacted_bundle_release_still_revalidates_omitted_resolution_records(monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace
    from uuid import uuid4

    import app.composition.school_fact_directory_gateway as module
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    live = _resolved_bundle_material_fixture()
    monkeypatch.setattr(module, "lookup", lambda *_args, **_kwargs: deepcopy(live))
    _, turn, _ = _directory_read_turn(SimpleNamespace(require_query=lambda **_kwargs: None))
    result = turn.execute(
        ModelToolCall(
            call_id="lookup",
            name=module.MODULE + ".lookup",
            arguments={
                "subject_kind": "organization",
                "query_text": "教务处",
                "include": ["official_contact"],
            },
        ),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    assert "results" not in result.model_content
    assert result.fact_card is not None and "results" in result.fact_card
    turn.revalidate("private-actor")
    assert result.history_check is not None
    assert result.history_check.valid() and result.history_check.refresh()
    live["results"][0]["records"][1]["fields"]["business_code"] = "changed-candidate-evidence"
    _assert_release_invalidated(turn, result)


def test_bundle_omits_repeated_static_selection_manual_but_keeps_failure_guidance():
    data = {
        "kind": "directory_bundle",
        "status": "ambiguous",
        "results": [
            {
                "kind": "directory_facts",
                "status": "known",
                "records": [{"reference": "location:one", "label": "青石"}],
                "query_support": {"guidance": "静态说明" * 3000},
            },
            {
                "kind": "directory_facts",
                "status": "unsupported_selection",
                "records": [],
                "query_support": {"guidance": "请使用关系引用"},
            },
        ],
    }
    result = directory_model_projection(data)
    assert "query_support" not in result["results"][0]
    assert result["results"][0]["records"] == data["results"][0]["records"]
    assert result["results"][1]["query_support"] == data["results"][1]["query_support"]
    assert "query_support" in data["results"][0]


@pytest.mark.parametrize("action", ["query", "lookup"])
@pytest.mark.parametrize("include_supporting_evidence", [False, True])
def test_read_tool_exposes_raw_provenance_only_for_requested_detail(
    monkeypatch, action, include_supporting_evidence
):
    from copy import deepcopy
    from uuid import uuid4

    import app.composition.school_fact_directory_gateway as module
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    provenance = {
        "basis_reference": "依据值",
        "approval_reference": "校办〔2026〕18号",
        "consent_reference": "授权书",
        "evidence_reference": "证据值",
    }
    view = {
        "kind": "directory_facts",
        "status": "known",
        "records": [
            {
                "reference": "f2:organization:abcdefgh",
                "fields": {"display_name": "教务处", **provenance},
                "source": {
                    "label": "正式发布",
                    "reference": "publication:1",
                    "published_at": "today",
                },
            }
        ],
        "quality": {"conflict": "open", "statements": [{"responsibility": "办公室", **provenance}]},
    }

    class Query:
        def query(self, **_kwargs):
            return deepcopy(view)

        def require_query(self, **_kwargs):
            pass

    monkeypatch.setattr(module, "lookup", lambda *_args, **_kwargs: deepcopy(view))
    _, turn, _ = _directory_read_turn(Query())
    arguments = (
        {"fact_kind": "organization", "query_text": "教务处"}
        if action == "query"
        else {
            "subject_kind": "organization",
            "query_text": "教务处",
            "include": ["organization_profile"],
        }
    )
    if include_supporting_evidence:
        arguments["include_supporting_evidence"] = True
    result = turn.execute(
        ModelToolCall(call_id=action, name=module.MODULE + "." + action, arguments=arguments),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    expected = provenance if include_supporting_evidence else {"available_fields": list(provenance)}
    assert result.model_content["records"][0]["supporting_evidence"] == {
        **expected, "source": {"label": "正式发布", "published_at": "today"}
    }
    quality = result.model_content["supporting_evidence"]["readable_quality"]
    assert quality["statements"][0]["supporting_evidence"] == expected
    assert (
        result.model_content["supporting_evidence"]["readable_quality"]["statements"][0]["details"]["发布与核实责任"]
        == "办公室"
    )
    assert quality["conflict"] == "正式资料存在待核实争议"
    assert result.fact_card["records"][0]["fields"] == view["records"][0]["fields"]
    assert result.fact_card["records"][0]["source"] == view["records"][0]["source"]
    assert result.fact_card["quality"] == view["quality"]
    turn.revalidate("private-actor")
    assert result.history_check.valid() and result.history_check.refresh()


@pytest.mark.parametrize("include_supporting_evidence", [False, True])
def test_unresolved_candidate_provenance_respects_detail_projection(include_supporting_evidence):
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import directory_model_material

    record = {
        "reference": "f2:organization:abcdefgh",
        "fields": {"display_name": "教务处", "basis_reference": "candidate-raw-provenance"},
    }
    view = {
        "kind": "directory_bundle",
        "status": "ambiguous",
        "results": [{"records": [record]}],
        "candidates": [deepcopy(record)],
    }
    material = directory_model_material(
        directory_model_projection(view), include_supporting_evidence=include_supporting_evidence
    )
    candidate = material["candidates"][0]
    assert candidate["fields"] == {"display_name": "教务处"}
    assert candidate["supporting_evidence"] == (
        {"basis_reference": "candidate-raw-provenance"}
        if include_supporting_evidence
        else {"available_fields": ["basis_reference"]}
    )
    assert view["candidates"][0]["fields"]["basis_reference"] == "candidate-raw-provenance"


@pytest.mark.parametrize(
    "kind,field,label",
    [
        ("official_contact", "service_hours", "服务时间"),
        ("organization", "short_name", "简称"),
        ("school_service", "audience_description", "服务对象"),
        ("organization_profile", "function_summary", "正式职能说明"),
    ],
)
def test_model_material_explains_unprovided_authorized_field_without_changing_fact_card(
    kind, field, label
):
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import (
        DIRECTORY_ASSISTANT_INSTRUCTION,
        directory_model_material,
    )

    record = {
        "fact_kind": kind,
        "reference": f"f2:{kind}:abcdefgh",
        "source": {"kind": "formal_publication", "label": "正式发布"},
        "fields": {field: None},
    }
    view = {"status": "known", "records": [record]}
    before = deepcopy(view)
    material = directory_model_material(view)
    assert material["records"][0]["missing_facts"][label] == "记录未提供"
    assert "组织" not in material["records"][0]["missing_facts"]
    assert "unset_facts" not in material["records"][0]
    assert material["status"] == "known"
    assert view == before
    assert "missing_facts" in DIRECTORY_ASSISTANT_INSTRUCTION
    assert "本条正式记录未提供" in DIRECTORY_ASSISTANT_INSTRUCTION
    assert "正式否定" in DIRECTORY_ASSISTANT_INSTRUCTION


@pytest.mark.parametrize("state", ["denied", "formal_negative", "unknown", "not_set"])
def test_other_field_states_are_not_relabelled_as_missing(state):
    from app.composition.school_fact_directory_gateway import directory_model_material

    record = {
        "fact_kind": "official_contact",
        "reference": "f2:official_contact:abcdefgh",
        "source": {"kind": "formal_publication"},
        "fields": {
            "owner_kind": "school",
            "contact_kind": "email",
            "contact_value": "office@example.invalid",
            "lifecycle_status": "active",
            "visibility_scope": "school",
            "audience_code": "staff",
            "service_hours": "08:00–17:00",
        },
        "field_states": {"service_hours": state, "audience_code": state},
    }
    material = directory_model_material({"records": [record]})["records"][0]
    assert not material.get("missing_facts")
    assert not material.get("unset_facts")
    assert "missing_fields" not in material["tool_context"]
    assert material["readable_facts"]["服务时间"] == "08:00–17:00"


def test_excerpt_and_task_hidden_evidence_are_not_marked_as_unset():
    from app.composition.school_fact_directory_gateway import directory_model_material

    records = [
        {
            "fact_kind": "policy_version",
            "reference": "f2:policy_version:abcdefgh",
            "source": {"kind": "formal_publication"},
            "fields": {"content_text": "制度原文甲乙丙丁"},
        },
        {
            "fact_kind": "organization_profile",
            "reference": "f2:organization_profile:abcdefgh",
            "source": {"kind": "formal_publication"},
            "fields": {
                "organization_type": "office",
                "function_summary": "负责教学事务",
                "basis_reference": "内部依据值",
            },
        },
    ]
    material = directory_model_material(directory_model_projection({"records": records}))
    assert "完整制度原文" not in material["records"][0].get("missing_facts", {})
    assert "正式依据" not in material["records"][1].get("missing_facts", {})
    assert "内部依据值" not in str(material)
    assert material["records"][0]["original_text"]["excerpt"] == "制度原文甲乙丙丁"


@pytest.mark.parametrize(
    "status",
    [
        "denied",
        "unsupported_selection",
        "source_unavailable",
        "no_current_records",
        "formal_negative",
    ],
)
def test_empty_or_unreadable_query_does_not_claim_missing_fields(status):
    from app.composition.school_fact_directory_gateway import directory_model_material

    view = {"fact_kind": "official_contact", "status": status, "records": []}
    material = directory_model_material(view)
    assert material == {
        "status": status,
        "records": [],
        "fact_type": "官方联系方式",
        "tool_context": {"fact_kind": "official_contact"},
    }


def test_model_names_distinguish_exact_selection_from_label_and_path_without_changing_card():
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import (
        DIRECTORY_ASSISTANT_INSTRUCTION,
        directory_model_material,
    )

    view = {
        "records": [
            {
                "fact_kind": "organization",
                "reference": "f2:organization:abcdefgh",
                "source": {"kind": "formal_publication"},
                "label": "展示简称",
                "fields": {
                    "display_name": "正式组织名",
                    "display_path": "学校/路径末段",
                    "short_name": "其他简称",
                },
            }
        ]
    }
    before = deepcopy(view)
    material = directory_model_material(view, include_routing_details=True)
    assert material["records"][0]["tool_context"]["exact_query_names"] == {
        "display_name": "正式组织名"
    }
    assert "label" not in material["records"][0]
    assert material["records"][0]["auxiliary_location"]["组织路径"] == "学校/路径末段"
    assert view == before
    assert "exact_query_names" in DIRECTORY_ASSISTANT_INSTRUCTION
    assert "路径末段" in DIRECTORY_ASSISTANT_INSTRUCTION
    assert "名称未匹配" in DIRECTORY_ASSISTANT_INSTRUCTION


@pytest.mark.parametrize(
    "count,coverage,truncated",
    [(1, "unknown", False), (3, "partial", True), (2, "complete", False)],
)
def test_model_result_count_is_authorized_set_not_reality(count, coverage, truncated):
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import directory_model_material

    view = {
        "kind": "directory_facts",
        "status": "known",
        "fact_kind": "official_contact",
        "records": [],
        "matched_count": count,
        "returned_count": 0,
        "truncated": truncated,
        "coverage": {"status": coverage, "description": "仅本次选择的获准记录"},
        "quality": {"evidence_status": "source_unavailable", "conflict": "open"},
    }
    before = deepcopy(view)
    material = directory_model_material(view, include_supporting_evidence=True)
    assert material["authorized_result_set"] == {
        "matched_count": count,
        "returned_count": 0,
        "truncated": truncated,
        "scope": "仅本次选择的获准记录",
        "counts_describe": "authorized_records_only",
    }
    assert (
        material["reality_coverage"]["meaning"]
        == {"unknown": "覆盖未确认", "partial": "部分覆盖", "complete": "声明范围完整覆盖"}[
            coverage
        ]
    )
    assert "matched_count" not in material and "coverage" not in material
    assert "quality" not in material.get("tool_context", {})
    assert view == before


def test_model_record_separates_readable_facts_from_tool_values_and_paths():
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import directory_model_material

    rows = [
        {
            "fact_kind": "organization",
            "reference": "f2:organization:distinct01",
            "source": {"kind": "formal_publication", "label": "正式发布"},
            "label": "展示别称",
            "fields": {
                "display_name": "课程发展中心",
                "display_path": "c/d",
                "business_code": "office-17",
                "parent_ref": "f2:organization:parent001",
            },
        },
        {
            "fact_kind": "organization_profile",
            "reference": "f2:organization_profile:distinct02",
            "source": {"kind": "formal_publication"},
            "fields": {
                "organization_type": "department",
                "function_summary": "统筹课程研究",
                "organization_ref": "f2:organization:distinct01",
            },
        },
        {
            "fact_kind": "official_contact",
            "reference": "f2:official_contact:distinct03",
            "source": {"kind": "formal_publication"},
            "fields": {"contact_kind": "email", "contact_value": "lesson@example.invalid"},
        },
        {
            "fact_kind": "person_contact",
            "reference": "f2:person_contact:distinct04",
            "source": {"kind": "formal_publication"},
            "fields": {"is_primary": False, "contact_value": "123"},
        },
    ]
    view = {"records": rows}
    before = deepcopy(view)
    material = directory_model_material(view, include_routing_details=True)["records"]
    assert material[0]["readable_facts"]["正式名称"] == "课程发展中心"
    assert "fields" not in material[0] and "label" not in material[0]
    assert material[0]["auxiliary_location"] == {"组织路径": "c/d"}
    assert "c/d" not in str(material[0]["readable_facts"])
    assert material[0]["tool_context"]["reference"] == rows[0]["reference"]
    assert material[0]["relationship_facts"][0]["target_ref"] == "f2:organization:parent001"
    assert "relations" not in material[0]["tool_context"]
    assert material[0]["tool_context"]["exact_query_names"] == {"display_name": "课程发展中心"}
    assert material[1]["readable_facts"]["正式组织类型"] == "部门"
    assert material[2]["missing_facts"]["服务时间"] == "记录未提供"
    assert material[3]["readable_facts"]["是否正式主要项"] is False
    assert view == before


def test_model_unrecognized_enum_is_not_invented_as_readable_fact():
    from app.composition.school_fact_directory_gateway import directory_model_material

    record = {
        "fact_kind": "organization_profile",
        "reference": "f2:organization_profile:distinct05",
        "source": {"kind": "formal_publication"},
        "fields": {"organization_type": "future_group", "function_summary": "负责资料研究"},
    }
    material = directory_model_material({"records": [record]})["records"][0]
    assert "正式组织类型" not in material["readable_facts"]
    assert material["uninterpreted_values"] == {"organization_type": "future_group"}
    assert material["readable_facts"]["正式职能说明"] == "负责资料研究"


@pytest.mark.parametrize(
    "field,value",
    [
        ("semantic_role_code", "department"),
        ("audience_code", "staff_scope_v9"),
        ("kind", "unknown_location_kind"),
    ],
)
def test_model_school_specific_codes_are_not_guessed_from_global_words(field, value):
    from app.composition.school_fact_directory_gateway import directory_model_material

    kind = {
        "semantic_role_code": "assignment",
        "audience_code": "official_contact",
        "kind": "location",
    }[field]
    record = {
        "fact_kind": kind,
        "reference": f"f2:{kind}:context001",
        "source": {"kind": "formal_publication"},
        "fields": {field: value},
    }
    result = directory_model_material({"records": [record]})["records"][0]
    assert result["uninterpreted_values"][field] == value
    assert value not in result["readable_facts"].values()


def test_model_path_without_formal_name_stays_auxiliary():
    from app.composition.school_fact_directory_gateway import directory_model_material

    record = {
        "fact_kind": "location",
        "reference": "f2:location:pathonly01",
        "source": {"kind": "formal_publication"},
        "label": "随手显示标签",
        "fields": {"display_path": "x/y"},
    }
    result = directory_model_material({"records": [record]}, include_routing_details=True)[
        "records"
    ][0]
    assert "正式名称" not in result["readable_facts"]
    assert result["tool_context"]["exact_query_names"] == {}
    assert result["auxiliary_location"] == {"地点路径": "x/y"}
    assert "随手显示标签" not in str(result)


@pytest.mark.parametrize("include_routing_details", [False, True])
def test_routing_details_only_enter_model_for_explicit_evidence(include_routing_details):
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import directory_model_material

    record = {
        "fact_kind": "organization",
        "reference": "f2:organization:route001",
        "source": {"kind": "formal_publication", "label": "正式发布", "published_at": "2026-08-01"},
        "fields": {
            "display_name": "课程研究室",
            "display_path": "演示学校/实验区/研究室",
            "business_code": "ROUTE-37",
            "basis_reference": "校办〔2026〕18号",
            "parent_ref": "f2:organization:parent01",
        },
    }
    view = {"records": [record]}
    before = deepcopy(view)
    material = directory_model_material(view, include_routing_details=include_routing_details)
    result = material["records"][0]
    assert "校办〔2026〕18号" not in str(material)
    assert result["supporting_evidence"] == {"available_fields": ["basis_reference"]}
    assert result["tool_context"]["reference"] == record["reference"]
    assert result["relationship_facts"][0]["target_ref"] == "f2:organization:parent01"
    assert "relations" not in result["tool_context"]
    assert result["tool_context"]["exact_query_names"] == {"display_name": "课程研究室"}
    assert result["source"] == {"label": "正式发布", "published_at": "2026-08-01"}
    if include_routing_details:
        assert result["auxiliary_location"] == {"组织路径": "演示学校/实验区/研究室"}
        assert result["tool_context"]["business_code"] == "ROUTE-37"
    else:
        assert "演示学校/实验区/研究室" not in str(material)
        assert "ROUTE-37" not in str(material)
        assert "auxiliary_location" not in result
        assert "business_code" not in result["tool_context"]
    assert view == before


def test_ambiguous_same_name_candidates_keep_references_without_routing_details():
    from app.composition.school_fact_directory_gateway import directory_model_material

    candidates = [
        {
            "fact_kind": "organization",
            "reference": f"f2:organization:choice{n:03}",
            "source": {"kind": "formal_publication"},
            "fields": {
                "display_name": "课程研究室",
                "display_path": f"校区{n}/研究室",
                "business_code": f"CHOICE-{n}",
            },
            "campuses": [{"reference": f"f2:location:campus{n:03}", "label": f"第{n}校区"}],
        }
        for n in (1, 2)
    ]
    result = directory_model_material(
        {"kind": "directory_bundle", "status": "ambiguous", "candidates": candidates}
    )
    assert result["status"] == "ambiguous"
    for n, candidate in zip((1, 2), result["candidates"], strict=True):
        assert candidate["tool_context"]["reference"] == f"f2:organization:choice{n:03}"
        assert candidate["tool_context"]["exact_query_names"] == {"display_name": "课程研究室"}
        assert candidate["campuses"] == candidates[n - 1]["campuses"]
        assert f"校区{n}/研究室" not in str(candidate) and f"CHOICE-{n}" not in str(candidate)


@pytest.mark.parametrize(
    "kind,fields",
    [
        ("assignment", {"position_title": "学科带头人"}),
        ("location_alias", {"alias": "创客室"}),
        ("organization_profile", {"function_summary": "组织课程研讨"}),
        ("school_service", {"display_name": "资料检索", "service_summary": "提供目录查询"}),
        ("organization_campus_relationship", {}),
    ],
)
def test_default_material_keeps_non_object_executable_exact_selectors(kind, fields):
    from app.composition.school_fact_directory_gateway import directory_model_material

    record = {
        "fact_kind": kind,
        "reference": f"f2:{kind}:selector01",
        "source": {"kind": "formal_publication"},
        "fields": fields,
    }
    result = directory_model_material({"records": [record]})["records"][0]
    assert result["tool_context"]["exact_query_names"] == fields


def test_model_fact_family_codes_are_tool_context_and_labels_are_readable():
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import directory_model_material

    view = _resolved_bundle_material_fixture()
    before = deepcopy(view)
    material = directory_model_material(view)
    assert material["tool_context"]["requested_fact_kinds"] == view["requested_fact_kinds"]
    assert "requested_fact_kinds" not in material
    assert material["requested_fact_types"] == ["官方联系方式", "组织职能"]
    assert material["status"] == "known"
    assert material["resolved_subject"]["fact_type"] == "组织"
    assert material["resolved_subject"]["tool_context"]["fact_kind"] == "organization"
    assert "fact_kind" not in material["resolved_subject"]
    assert material["facts"][0]["fact_type"] == "官方联系方式"
    assert material["facts"][0]["tool_context"]["fact_kind"] == "official_contact"
    assert "fact_kind" not in material["facts"][0]
    assert material["facts"][1]["status"] == "denied"
    assert material["facts"][1]["fact_type"] == "组织职能"
    assert material["resolution_checks"][0]["tool_context"]["fact_kind"] == "organization"
    assert material["resolution_checks"][0]["readable_quality"] == {
        "source_statement": "未取得适用的来源可用性声明，原来源状态尚不能确定",
        "statements": [],
    }
    assert view == before


def test_model_family_label_does_not_rewrite_unrecognized_or_non_query_contracts():
    from app.composition.school_fact_directory_gateway import directory_model_material

    for view in (
        {"status": "rejected", "fact_kind": "unknown_family", "records": []},
        {"kind": "fact_fields", "fact_kind": "organization", "fields": []},
    ):
        assert directory_model_material(view) == view


@pytest.mark.parametrize(
    "fact_kind",
    [
        "official_contact",
        "organization_profile",
        "organization_location",
        "person_contact",
        "policy",
        "school_service",
    ],
)
@pytest.mark.parametrize("status", ["no_current_records", "formal_negative", "known"])
def test_fact_status_and_reality_coverage_do_not_need_a_model_process_code(fact_kind, status):
    from app.composition.school_fact_directory_gateway import directory_model_material

    value = {
        "kind": "directory_facts",
        "fact_kind": fact_kind,
        "status": status,
        "records": [],
        "truncated": False,
        "query": {"scope": "selected", "related_ref": "organization:one", "query_text": None},
        "coverage": {"status": "unknown"},
    }
    material = directory_model_material(value)
    assert "query_completion" not in material["tool_context"]
    assert "query_completion" not in material
    assert material["query_scope"] == {"scope": "selected", "related_ref": "organization:one"}
    assert material["status"] == status
    assert value.get("query_completion") is None


@pytest.mark.parametrize(
    "extra",
    [
        {"status": "denied"},
        {"status": "source_unavailable"},
        {"status": "ambiguous"},
        {"truncated": True},
        {"quality": {"evidence_status": "source_unavailable"}},
    ],
)
def test_omitting_process_code_keeps_partial_or_failed_evidence(extra):
    from app.composition.school_fact_directory_gateway import directory_model_material

    value = {
        "kind": "directory_facts",
        "fact_kind": "policy",
        "status": "no_current_records",
        "records": [],
        **extra,
    }
    material = directory_model_material(value)
    assert "query_completion" not in material["tool_context"]
    assert "query_completion" not in material
    assert material["status"] == value["status"]
    if extra.get("truncated"):
        assert material["fact_reading"]["limit"] == "本次获准结果未全部返回"
    if "quality" in extra:
        assert material["readable_quality"]["reading"] == "本次未能读取质量声明"


def test_projection_truncation_invalidates_bundle_query_completion():
    from app.composition.school_fact_directory_gateway import (
        directory_model_projection,
    )

    value = _resolved_bundle_material_fixture()
    value["query_completion"] = "completed"
    value["results"] = value["results"][:2]
    value["results"][-1]["records"][0]["fields"]["contact_value"] = "x" * 12000
    from app.composition.school_fact_product_contacts import ContactProductUnavailable

    with pytest.raises(ContactProductUnavailable):
        directory_model_projection(value)
    assert value["query_completion"] == "completed"


def _relationship_record(kind, key, **fields):
    return {
        "fact_kind": kind,
        "reference": f"f2:{kind}:{key}",
        "label": "不得用作正式名称的展示标签",
        "fields": fields,
        "source": {"kind": "formal_publication", "label": "正式发布"},
    }


@pytest.mark.parametrize(
    "kind,fields,expected",
    [
        (
            "school_service",
            {"organization_ref": "f2:organization:o"},
            [("服务提供组织", "f2:organization:o")],
        ),
        (
            "organization_campus_relationship",
            {"organization_ref": "f2:organization:o", "campus_ref": "f2:location:c"},
            [("组织", "f2:organization:o"), ("关联校区", "f2:location:c")],
        ),
        (
            "person_workplace",
            {"person_ref": "f2:person:p", "location_ref": "f2:location:r"},
            [("人员", "f2:person:p"), ("办公地点", "f2:location:r")],
        ),
    ],
)
def test_formal_relationships_remain_facts_with_readable_predicates(kind, fields, expected):
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import directory_model_material

    record = _relationship_record(kind, "one", **fields)
    view = {"kind": "directory_facts", "fact_kind": kind, "status": "known", "records": [record]}
    before = deepcopy(view)
    material = directory_model_material(view)
    result = material["records"][0]
    assert [
        (edge["relation"], edge["target_ref"]) for edge in result["relationship_facts"]
    ] == expected
    assert all("target_name" not in edge for edge in result["relationship_facts"])
    assert "relations" not in result["tool_context"]
    assert {edge["target_ref"] for edge in result["relationship_facts"]} == set(fields.values())
    assert view == before


def test_relationship_target_name_requires_an_unambiguous_authorized_formal_name():
    from app.composition.school_fact_directory_gateway import directory_model_material

    service = _relationship_record("school_service", "s", organization_ref="f2:organization:o")
    organization = _relationship_record("organization", "o", display_name="教务处")
    base = {"kind": "directory_facts", "status": "known", "records": [service, organization]}
    edge = directory_model_material(base)["records"][0]["relationship_facts"][0]
    assert edge == {
        "subject_ref": "f2:school_service:s",
        "target_type": "组织",
        "relation": "服务提供组织",
        "target_ref": "f2:organization:o",
        "target_name": "教务处",
        "meaning": "仅确定服务提供组织，不确定服务适用或排除哪些校区。",
    }
    conflicting = _relationship_record("organization", "o", display_name="其他名称")
    base["records"].append(conflicting)
    edge = directory_model_material(base)["records"][0]["relationship_facts"][0]
    assert "target_name" not in edge
    base["records"] = [
        service,
        _relationship_record("organization", "o", display_path="学校/教务处"),
    ]
    assert (
        "target_name" not in directory_model_material(base)["records"][0]["relationship_facts"][0]
    )


def test_service_relationship_does_not_invent_an_unqueried_campus_relationship():
    from app.composition.school_fact_directory_gateway import directory_model_material

    view = {
        "kind": "directory_facts",
        "fact_kind": "school_service",
        "status": "known",
        "records": [
            _relationship_record(
                "school_service", "s", display_name="咨询服务", organization_ref="f2:organization:o"
            )
        ],
        "query": {"scope": "all_authorized"},
    }
    material = directory_model_material(view)
    assert material["records"][0]["relationship_facts"] == [
        {
            "subject_ref": "f2:school_service:s",
            "subject_name": "咨询服务",
            "target_type": "组织",
            "relation": "服务提供组织",
            "target_ref": "f2:organization:o",
            "meaning": "仅确定服务提供组织，不确定服务适用或排除哪些校区。",
        }
    ]
    assert material["query_scope"] == {"scope": "all_authorized"}
    assert "campus" not in str(material)


@pytest.mark.parametrize("status", ["no_current_records", "formal_negative", "denied"])
def test_absent_relationship_records_keep_their_query_evidence_without_synthetic_edges(status):
    from app.composition.school_fact_directory_gateway import directory_model_material

    view = {
        "kind": "directory_facts",
        "fact_kind": "organization_campus_relationship",
        "status": status,
        "records": [],
        "query": {"scope": "selected", "related_ref": "f2:organization:o"},
    }
    material = directory_model_material(view)
    assert material["status"] == status
    assert material["records"] == []
    assert "relationship_facts" not in material
    assert material["query_scope"]["related_ref"] == "f2:organization:o"


def test_relationship_material_keeps_service_ownership_and_campus_affiliation_as_separate_edges():
    from app.composition.school_fact_directory_gateway import directory_model_material

    service = _relationship_record("school_service", "s", organization_ref="f2:organization:o")
    relation = _relationship_record(
        "organization_campus_relationship",
        "oc",
        organization_ref="f2:organization:o",
        campus_ref="f2:location:c",
    )
    campus = _relationship_record("location", "c", display_name="南校区")
    material = directory_model_material({"records": [service, relation, campus]})
    assert material["records"][0]["relationship_facts"] == [
        {
            "subject_ref": "f2:school_service:s",
            "target_type": "组织",
            "relation": "服务提供组织",
            "target_ref": "f2:organization:o",
            "meaning": "仅确定服务提供组织，不确定服务适用或排除哪些校区。",
        }
    ]
    assert material["records"][1]["relationship_facts"][1] == {
        "subject_ref": "f2:organization_campus_relationship:oc",
        "target_type": "校区与地点",
        "relation": "关联校区",
        "target_ref": "f2:location:c",
        "target_name": "南校区",
        "meaning": (
            "仅证明组织与此校区的正式关联，不声明关联唯一，"
            "也不规定该组织所提供服务的适用或排除范围。"
        ),
    }
    assert "relationship_facts" not in material["records"][2]


def test_relationship_material_does_not_treat_unrecognized_records_as_authorized_names():
    from app.composition.school_fact_directory_gateway import directory_model_material

    service = _relationship_record("school_service", "s", organization_ref="f2:organization:o")
    alleged_target = _relationship_record("organization", "o", display_name="未核实名称")
    alleged_target.pop("source")
    material = directory_model_material({"records": [service, alleged_target]})
    assert "target_name" not in material["records"][0]["relationship_facts"][0]


@pytest.mark.parametrize(
    "source,evidence,expected_reading,expected_source",
    [
        ("unavailable", "known", "已取得质量声明", "声明记载原来源不可用"),
        (
            "unknown",
            "source_unavailable",
            "本次未能读取质量声明",
            "未取得适用的来源可用性声明，原来源状态尚不能确定",
        ),
        (
            "unknown",
            "forbidden",
            "当前身份无权读取质量声明",
            "未取得适用的来源可用性声明，原来源状态尚不能确定",
        ),
        (
            "unknown",
            "no_current_records",
            "本次未返回有效质量声明",
            "未取得适用的来源可用性声明，原来源状态尚不能确定",
        ),
    ],
)
def test_quality_material_separates_reading_from_declared_source_state(
    source, evidence, expected_reading, expected_source
):
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import directory_model_material

    view = {
        "kind": "directory_facts",
        "status": "known",
        "records": [],
        "factual_at": "2026-09-11T10:00:00Z",
        "quality": {
            "source_availability": source,
            "evidence_status": evidence,
            "coverage": "partial",
            "conflict": "open",
            "statements": [],
        },
        "returned_count": 0,
        "coverage": {"status": "partial", "description": "本次指定组织"},
    }
    before = deepcopy(view)
    material = directory_model_material(view)
    assert "quality" not in material
    assert "quality" not in material.get("tool_context", {})
    readable = material["readable_quality"]
    assert readable["reading"] == expected_reading
    assert readable["source_statement"] == expected_source
    assert readable["coverage"] == "部分覆盖；不支持全部或唯一"
    assert readable["conflict"] == "正式资料存在待核实争议"
    assert material["factual_at"] == view["factual_at"]
    assert material["reality_coverage"] == {"meaning": "部分覆盖"}
    assert material["reality_coverage"]["meaning"] == "部分覆盖"
    assert view == before


def test_quality_claim_material_preserves_narrow_scope_time_and_authorized_names():
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import directory_model_material

    organization = _relationship_record("organization", "quality-owner", display_name="综合事务组")
    claim = {
        "reference": "f2:fact_quality:claim",
        "kind": "coverage",
        "owner_kind": "organization",
        "owner_ref": organization["reference"],
        "target_ref": None,
        "target_fact_kind": "official_contact",
        "applies_to_query": False,
        "applies_to_records": [],
        "purpose": "reception",
        "coverage_status": "partial",
        "valid_from": "2026-08-01T00:00:00Z",
        "valid_to": "2026-10-01T00:00:00Z",
        "responsibility": "正式资料维护组",
    }
    view = {"records": [organization], "quality": {"statements": [claim]}}
    before = deepcopy(view)
    material = directory_model_material(view)
    statement = material["readable_quality"]["statements"][0]
    assert statement["kind"] == "覆盖声明"
    assert "owner_filter_name" not in statement["scope"]
    assert statement["tool_context"]["owner_ref"] == organization["reference"]
    assert statement["scope"]["application"] == "仅限声明列明的对象与条件"
    assert statement["conditions"] == {"地点用途": "接待"}
    assert (
        statement["valid_from"] == claim["valid_from"]
        and statement["valid_to"] == claim["valid_to"]
    )
    assert statement["details"]["发布与核实责任"] == "正式资料维护组"
    operands = statement["tool_context"]
    assert operands["reference"] == claim["reference"]
    assert operands["owner_ref"] == claim["owner_ref"]
    assert statement["scope"]["application"] == "仅限声明列明的对象与条件"
    assert "valid_from" not in operands and "responsibility" not in operands
    assert view == before


def test_relationship_predicate_meaning_does_not_grant_service_campus_exclusion():
    from app.composition.school_fact_directory_gateway import directory_model_material

    service = _relationship_record(
        "school_service", "service", organization_ref="f2:organization:provider"
    )
    relationship = _relationship_record(
        "organization_campus_relationship",
        "membership",
        organization_ref="f2:organization:provider",
        campus_ref="f2:location:campus",
    )
    material = directory_model_material({"records": [service, relationship]})
    provider = material["records"][0]["relationship_facts"][0]
    affiliation = material["records"][1]["relationship_facts"][1]
    assert "不确定服务适用或排除哪些校区" in provider["meaning"]
    assert "不声明关联唯一" in affiliation["meaning"]
    assert "不规定该组织所提供服务的适用或排除范围" in affiliation["meaning"]


@pytest.mark.parametrize(
    "status", ["unavailable", "available", "unknown", "conflicting_statements"]
)
def test_completed_read_keeps_formal_source_risk_distinct_from_dependency_failure(status):
    from app.composition.school_fact_directory_gateway import (
        _evidence_complete,
        directory_model_material,
    )

    view = {
        "kind": "directory_facts",
        "status": "known",
        "records": [],
        "quality": {"source_availability": status, "evidence_status": "known", "statements": []},
    }
    material = directory_model_material(view)
    assert "query_completion" not in material["tool_context"]
    assert material["readable_quality"]["reading"] == "已取得质量声明"
    assert _evidence_complete(view)
    view["quality"]["evidence_status"] = "source_unavailable"
    unavailable = directory_model_material(view)
    assert "query_completion" not in unavailable["tool_context"]
    assert unavailable["readable_quality"]["reading"] == "本次未能读取质量声明"
    assert (
        unavailable["readable_quality"]["source_statement"]
        == material["readable_quality"]["source_statement"]
    )
    assert not _evidence_complete(view)


def test_model_quality_evidence_capture_uses_raw_card_at_initial_and_refresh_boundary(monkeypatch):
    from contextlib import contextmanager
    from copy import deepcopy
    from types import SimpleNamespace
    from uuid import uuid4

    from app.composition import school_fact_directory_gateway as module
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    view = {
        "kind": "directory_facts",
        "status": "known",
        "records": [],
        "quality": {"source_availability": "unavailable", "evidence_status": "known"},
    }
    alive = [False]
    seen = []

    class Backend:
        @contextmanager
        def capture(self, **kwargs):
            yield SimpleNamespace(seal=lambda: SimpleNamespace(check=lambda actor: alive[0]))

    class Query:
        evidence = Backend()

        def query(self, **kwargs):
            return deepcopy(view)

        def require_query(self, **kwargs):
            pass

    def inspect(value):
        seen.append(deepcopy(value))
        assert "quality" in value and "readable_quality" not in value
        return True

    monkeypatch.setattr(module, "_evidence_complete", inspect)
    _, turn, _ = _directory_read_turn(Query())
    result = turn.execute(
        ModelToolCall(
            call_id="quality", name=module.MODULE + ".query", arguments={"fact_kind": "policy"}
        ),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    assert result.history_check is not None
    assert not result.history_check.refresh()
    assert len(seen) == 2
    assert result.fact_card["quality"] == view["quality"]
    assert "quality" not in result.model_content


@pytest.mark.parametrize("names", [[], ["名称甲", "名称乙"]])
def test_quality_scope_does_not_invent_unknown_or_conflicting_authorized_names(names):
    from app.composition.school_fact_directory_gateway import directory_model_material

    records = [_relationship_record("organization", "one", display_name=name) for name in names]
    claim = {
        "kind": "coverage",
        "owner_ref": "f2:organization:one",
        "applies_to_query": False,
        "coverage_status": "future-unrecognized-status",
        "valid_from": "2026-09-01T00:00:00Z",
    }
    view = {"records": records, "quality": {"statements": [claim]}}
    readable = directory_model_material(view)["readable_quality"]["statements"][0]
    assert "scope_owner_name" not in readable["scope"]
    assert readable["details"]["覆盖状态"] == "该状态尚无领域释义，不能据此作结论"


def test_bounded_composite_card_is_not_rejected_before_material_compaction():
    from app.composition.school_fact_directory_gateway import directory_model_material

    records = [
        _relationship_record(
            "organization", f"long-{n}", display_name=f"机构{n}", business_code="internal-" * 850
        )
        for n in range(2)
    ]
    value = {
        "kind": "directory_bundle",
        "status": "known",
        "subject_label": None,
        "results": [
            {
                "kind": "directory_facts",
                "fact_kind": "organization",
                "status": "known",
                "records": [record],
            }
            for record in records
        ],
    }
    projected = directory_model_projection(value)
    assert len(projected["results"]) == 2
    assert len(directory_model_material(projected)["results"]) == 2


def test_bundle_keeps_model_material_limit_after_evidence_separation():
    from app.composition.school_fact_directory_gateway import directory_model_material
    from app.composition.school_fact_product_contacts import ContactProductUnavailable

    value = {
        "kind": "directory_bundle",
        "status": "known",
        "subject_label": None,
        "results": [
            {
                "kind": "directory_facts",
                "fact_kind": "organization",
                "status": "known",
                "records": [
                    _relationship_record("organization", f"large-{n}", display_name="正文" * 1500)
                ],
            }
            for n in range(9)
        ],
    }
    with pytest.raises(ContactProductUnavailable):
        directory_model_material(directory_model_projection(value))


def test_service_material_binds_components_without_inventing_service_campus_scope():
    from app.composition.school_fact_directory_gateway import directory_model_material
    from app.composition.school_fact_directory_lookup import FactLookupInput, lookup
    from tests.test_f2_directory_product import actor
    from tests.test_f2_service_evidence import ServiceFacts

    source = ServiceFacts()
    value = lookup(
        source,
        actor=actor(),
        payload=FactLookupInput(
            subject_kind="school_service",
            scope="all_authorized",
            include=("official_contact", "organization_campus_relationship"),
        ),
    )
    for view in value["results"]:
        for row in view.get("records", []):
            row["fact_kind"] = view["fact_kind"]
            row["source"] = {"kind": "formal_publication", "label": "正式发布"}
    material = directory_model_material(directory_model_projection(value))
    assert "service_groups" not in material
    group = material["service_results"][0]
    assert group["service_name"] == "资料咨询"
    assert group["official_contact"]["outcome"] == "已取得正式记录"
    assert (
        group["official_contact"]["tool_context"]["reference"] == "f2:official_contact:contact000"
    )
    assert "不确定服务适用或排除" in material["relationship_scope"]
    assert "advice@example.invalid" in str(material)
    assert group["facts"]["readable_facts"]["正式名称"] == "资料咨询"


def test_quality_sharing_requires_identical_scope_time_and_authorized_projection():
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import _share_bundle_quality

    first = {
        "readable_quality": {"statement": "核实" * 120},
        "reality_coverage": {"meaning": "本范围"},
        "tool_context": {
            "quality": {"owner_ref": "f2:organization:first", "valid_from": "2026-09-10"}
        },
    }
    different = deepcopy(first)
    different["tool_context"]["quality"]["owner_ref"] = "f2:organization:other"
    value = {"kind": "directory_bundle", "results": [first, deepcopy(first), different]}
    original = deepcopy(value)
    _share_bundle_quality(value)
    assert (
        value["results"][0]["quality_profile_index"]
        == value["results"][1]["quality_profile_index"]
        == 0
    )
    assert "quality_profile_index" not in value["results"][2]
    assert value["quality_profiles"][0] == original["results"][0]


def test_quality_task_material_has_one_semantic_copy_and_keeps_complete_claim_scope():
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import directory_model_material

    claim = {
        "reference": "f2:fact_quality:example",
        "kind": "source",
        "owner_kind": "organization",
        "owner_ref": "f2:organization:owner",
        "target_fact_kind": "official_contact",
        "target_ref": "f2:official_contact:target",
        "applies_to_query": False,
        "applies_to_records": ["f2:official_contact:target", "f2:official_contact:other"],
        "source_status": "unavailable",
        "purpose": "office",
        "contact_kind": "phone",
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to": "2026-09-15T00:00:00Z",
        "responsibility": "值班负责人",
        "replacement_ref": "f2:official_contact:replacement",
    }
    view = {
        "records": [],
        "quality": {
            "source_availability": "unavailable",
            "evidence_status": "known",
            "statements": [claim],
        },
    }
    before = deepcopy(view)
    material = directory_model_material(view)
    assert "quality" not in material.get("tool_context", {})
    statement = material["readable_quality"]["statements"][0]
    operands = statement["tool_context"]
    for field in ("reference", "owner_ref", "target_ref", "applies_to_records", "replacement_ref"):
        assert operands[field] == claim[field]
    assert "source_status" not in operands and "kind" not in operands
    assert statement["scope"]["application"] == "仅限声明列明的对象与条件"
    assert statement["scope"]["owner_filter_type"] == "组织"
    assert statement["scope"]["target_fact_type"] == "官方联系方式"
    assert statement["conditions"] == {"地点用途": "办公", "联系类型": "电话"}
    assert statement["details"]["来源可用状态"] == "声明记载原来源不可用"
    assert (
        statement["valid_from"] == claim["valid_from"]
        and statement["valid_to"] == claim["valid_to"]
    )
    assert view == before


def test_service_component_does_not_claim_complete_after_channel_display_is_limited():
    from app.composition.school_fact_directory_gateway import directory_model_material

    value = {
        "kind": "directory_bundle",
        "status": "known",
        "subject_label": None,
        "results": [
            {
                "kind": "directory_facts",
                "fact_kind": "official_contact",
                "status": "known",
                "records": [
                    _relationship_record("official_contact", "oversize", contact_value="x" * 12000)
                ],
            }
        ],
        "service_groups": [
            {
                "service_reference": "f2:school_service:one",
                "official_contact": {
                    "status": "known",
                    "reference": "f2:official_contact:oversize",
                },
            }
        ],
    }
    from app.composition.school_fact_product_contacts import ContactProductUnavailable

    with pytest.raises(ContactProductUnavailable):
        directory_model_material(directory_model_projection(value))


@pytest.mark.parametrize(
    "read_state", ["source_unavailable", "denied", "not_queried", "no_current_records"]
)
def test_unknown_source_statement_does_not_assert_statement_absence(read_state):
    from app.composition.school_fact_directory_gateway import directory_model_material

    view = {
        "kind": "directory_facts",
        "status": "known",
        "records": [],
        "quality": {"source_availability": "unknown", "evidence_status": read_state},
    }
    material = directory_model_material(view)
    assert (
        material["readable_quality"]["source_statement"]
        == "未取得适用的来源可用性声明，原来源状态尚不能确定"
    )


def test_source_detail_does_not_expand_routing_metadata():
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import directory_model_material

    record = _relationship_record(
        "organization",
        "source-detail",
        display_name="课程研究室",
        display_path="示例学校/实验区/研究室",
        business_code="ROUTE-37",
        basis_reference="校办〔2026〕18号",
        approval_reference="opaque:approval-42",
    )
    before = deepcopy(record)
    result = directory_model_material({"records": [record]}, include_supporting_evidence=True)[
        "records"
    ][0]
    assert "auxiliary_location" not in result
    assert "business_code" not in result["tool_context"]
    assert result["supporting_evidence"] == {
        "basis_reference": "校办〔2026〕18号",
        "approval_reference": "opaque:approval-42",
    }
    assert result["source"] == {"label": "正式发布"}
    assert record == before


@pytest.mark.parametrize("action", ["query", "lookup"])
def test_routing_details_have_an_independent_opt_in(action):
    from app.composition.school_fact_directory_gateway import DirectoryFactToolGateway
    from app.composition.school_fact_directory_lookup import FactLookupInput
    from app.composition.school_fact_directory_product import FactQueryInput

    schema = FactQueryInput if action == "query" else FactLookupInput
    parameters = schema.model_json_schema()["properties"]
    assert parameters.get("include_routing_details", {}).get("default") is False
    descriptor = next(
        item for item in DirectoryFactToolGateway.descriptors if item.name.endswith("." + action)
    )
    assert "include_routing_details" in descriptor.parameters["properties"]


@pytest.mark.parametrize(
    "kind,code,label",
    [
        ("official_contact", "audience_code", "明确对象范围依据"),
        ("organization", "business_code", "业务编码"),
    ],
)
def test_unprovided_uninterpreted_codes_remain_tool_states(kind, code, label):
    from app.composition.school_fact_directory_gateway import directory_model_material

    record = _relationship_record(kind, "missing-code")
    result = directory_model_material({"records": [record]})["records"][0]
    assert label not in result["missing_facts"]
    assert code in result["tool_context"]["missing_fields"]
    if kind == "official_contact":
        assert result["missing_facts"]["服务时间"] == "记录未提供"


@pytest.mark.parametrize("action", ["query", "lookup"])
@pytest.mark.parametrize("detail", ["include_supporting_evidence", "include_routing_details"])
def test_detailed_read_history_keeps_default_facts_and_complete_invalidation(
    monkeypatch, action, detail
):
    from copy import deepcopy
    from uuid import uuid4

    import app.composition.school_fact_directory_gateway as module
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    record = _relationship_record(
        "school_service",
        "history-detail",
        display_name="资料问询",
        organization_ref="f2:organization:provider",
        basis_reference="校办〔2026〕18号",
    )
    live = {
        "kind": "directory_facts",
        "fact_kind": "school_service",
        "status": "known",
        "records": [
            record,
            _relationship_record(
                "organization",
                "provider",
                display_name="课程研究室",
                display_path="示例学校/实验区/研究室",
                business_code="ROUTE-37",
            ),
        ],
        "quality": {
            "evidence_status": "known",
            "conflict": "open",
            "source_availability": "unknown",
            "statements": [{"kind": "source", "basis_reference": "quality:review-17"}],
        },
    }

    class Query:
        def query(self, **_kwargs):
            return deepcopy(live)

        def require_query(self, **_kwargs):
            pass

    monkeypatch.setattr(module, "lookup", lambda *_args, **_kwargs: deepcopy(live))
    _, turn, _ = _directory_read_turn(Query())
    arguments = (
        {"fact_kind": "school_service", "reference": record["reference"]}
        if action == "query"
        else {
            "subject_kind": "school_service",
            "reference": record["reference"],
            "include": ["official_contact"],
        }
    )
    arguments[detail] = True
    result = turn.execute(
        ModelToolCall(call_id=action, name=module.MODULE + "." + action, arguments=arguments),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    assert result.history_content is not None
    service = result.model_content["records"][0]
    provider = result.model_content["records"][1]
    if detail == "include_supporting_evidence":
        assert service["supporting_evidence"] == {
            "basis_reference": "校办〔2026〕18号", "source": {"label": "正式发布"}
        }
        assert "auxiliary_location" not in provider
    else:
        assert service["supporting_evidence"] == {
            "available_fields": ["basis_reference"], "source": {"label": "正式发布"}
        }
        assert provider["auxiliary_location"] == {"组织路径": "示例学校/实验区/研究室"}
        assert provider["tool_context"]["business_code"] == "ROUTE-37"
    assert result.history_content == module.answer_evidence_material(
        module.directory_history_material(result.fact_card)
    )
    for detailed_value in (
        "校办〔2026〕18号",
        "quality:review-17",
        "ROUTE-37",
        "示例学校/实验区/研究室",
    ):
        assert detailed_value not in str(result.history_content)
    retained = result.history_content["records"][0]
    assert result.history_content["publication_sources"][retained["publication_source_index"]] == {
        "label": "正式发布"
    }
    assert retained["relationship_facts"][0]["target_ref"] == "f2:organization:provider"
    assert retained["tool_context"]["reference"] == record["reference"]
    history_quality = result.history_content["supporting_evidence"]["readable_quality"]
    assert history_quality["conflict"] == "正式资料存在待核实争议"
    assert result.fact_card["records"][0]["fields"]["basis_reference"] == "校办〔2026〕18号"
    assert result.history_check is not None and result.history_check.valid()
    live["records"][0]["fields"]["basis_reference"] = "校办〔2026〕19号"
    _assert_release_invalidated(turn, result)


def test_actual_directory_history_replays_sources_without_detailed_tool_values():
    import json
    from copy import deepcopy
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE
    from app.platform.assistant.runtime import AssistantRuntime
    from app.platform.assistant.tool_loop import AssistantToolLoop, ModelReply, ModelToolCall
    from tests.assistant.test_tool_loop import ScriptModel

    record = _relationship_record(
        "organization",
        "history-owner",
        display_name="课程研究室",
        display_path="示例学校/实验区/研究室",
        business_code="ROUTE-37",
        basis_reference="校办〔2026〕18号",
    )
    live = {"kind": "directory_facts", "status": "known", "records": [record]}

    class Query:
        def query(self, **_kwargs):
            return deepcopy(live)

    gateway, _, _ = _directory_read_turn(Query())
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("private-actor", uuid4())
    accepted = runtime.accept_turn(
        conversation.conversation_ref, "private-actor", uuid4(), "核对原始依据和路径"
    )
    first = ScriptModel(
        [
            ModelReply(
                calls=(
                    ModelToolCall(
                        call_id="details",
                        name=MODULE + ".query",
                        arguments={
                            "fact_kind": "organization",
                            "reference": record["reference"],
                            "include_supporting_evidence": True,
                            "include_routing_details": True,
                        },
                    ),
                )
            ),
            ModelReply(text="已取得所问原始字段，可继续核对发布来源。"),
        ]
    )
    AssistantToolLoop(model=cast(ToolModel, first), gateway_factory=gateway.for_turn).run_accepted(
        runtime=runtime, accepted=accepted, actor_context_ref="private-actor", request_id=uuid4()
    )
    for raw in ("校办〔2026〕18号", "ROUTE-37", "示例学校/实验区/研究室"):
        assert raw in first.requests[1].messages[-1].content
    follow = runtime.accept_turn(
        conversation.conversation_ref, "private-actor", uuid4(), "发布来源呢？"
    )
    second = ScriptModel([ModelReply(text="该记录来自学校正式发布资料。")])
    AssistantToolLoop(model=cast(ToolModel, second), gateway_factory=gateway.for_turn).run_accepted(
        runtime=runtime, accepted=follow, actor_context_ref="private-actor", request_id=uuid4()
    )
    quoted = json.loads(second.requests[0].messages[1].content)["turns"][-1]
    retained = quoted["quoted_tool_results"][0]["records"][0]
    assert retained["supporting_evidence"]["source"] == {"label": "正式发布"}
    assert retained["tool_context"]["reference"] == record["reference"]
    assert retained["readable_facts"]["正式名称"] == "课程研究室"
    for raw in ("校办〔2026〕18号", "ROUTE-37", "示例学校/实验区/研究室"):
        assert raw not in str(quoted)
    record["fields"]["basis_reference"] = "校办〔2026〕19号"
    changed = runtime.accept_turn(
        conversation.conversation_ref, "private-actor", uuid4(), "刚才资料还有吗？"
    )
    third = ScriptModel([ModelReply(text="原依据已变化，需要重新核对。")])
    AssistantToolLoop(model=cast(ToolModel, third), gateway_factory=gateway.for_turn).run_accepted(
        runtime=runtime, accepted=changed, actor_context_ref="private-actor", request_id=uuid4()
    )
    turns = json.loads(third.requests[0].messages[1].content)["turns"]
    assert all(turn.get("history_context_unavailable") for turn in turns)
    assert all("quoted_tool_results" not in turn for turn in turns)


def test_default_read_supplies_history_without_redundant_tool_fields():
    from copy import deepcopy
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    record = _relationship_record(
        "school_service",
        "history-default",
        display_name="资料问询",
        organization_ref="f2:organization:provider",
        unresolved_ref="f2:organization:other",
        basis_reference="校办〔2026〕18号",
    )
    record.update(valid_from="2026-09-01T00:00:00Z", valid_to=None, version=2)
    live = {
        "kind": "directory_facts",
        "fact_kind": "school_service",
        "status": "known",
        "records": [record],
        "queried_at": "2026-09-12T00:00:00Z",
        "factual_at": "2026-09-11T00:00:00Z",
        "query": {"scope": "selected", "reference": record["reference"]},
        "matched_count": 1,
        "returned_count": 1,
        "truncated": False,
        "next_cursor": None,
        "coverage": {"status": "partial", "description": "仅所选服务记录"},
        "quality": {"evidence_status": "known", "conflict": "open", "coverage": "partial"},
    }

    class Query:
        def query(self, **_kwargs):
            return deepcopy(live)

    _, turn, _ = _directory_read_turn(Query())
    result = turn.execute(
        ModelToolCall(
            call_id="default",
            name=MODULE + ".query",
            arguments={"fact_kind": "school_service", "reference": record["reference"]},
        ),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    assert result.history_content is not None
    history = result.history_content
    retained = history["records"][0]
    assert retained["readable_facts"] == result.model_content["records"][0]["readable_facts"]
    assert retained["missing_facts"] == result.model_content["records"][0]["missing_facts"]
    assert retained["missing_facts"]["服务对象"] == "记录未提供"
    assert (
        retained["relationship_facts"] == result.model_content["records"][0]["relationship_facts"]
    )
    assert retained["tool_context"]["reference"] == record["reference"]
    assert retained["tool_context"]["relations"] == {"unresolved_ref": "f2:organization:other"}
    assert "exact_query_names" not in retained["tool_context"]
    assert "missing_fields" not in retained["tool_context"]
    assert retained["supporting_evidence"] == {
        "source": result.model_content["records"][0]["supporting_evidence"]["source"]
    }
    assert retained["valid_from"] == record["valid_from"] and retained["valid_to"] is None
    assert history["factual_at"] == live["factual_at"]
    assert history["query_scope"] == live["query"]
    assert history["authorized_result_set"]["matched_count"] == 1
    assert history["authorized_result_set"]["scope"] == "仅所选服务记录"
    assert (
        history["supporting_evidence"]["readable_quality"]
        == result.model_content["supporting_evidence"]["readable_quality"]
    )
    assert history["reality_coverage"] == result.model_content["reality_coverage"]
    assert result.fact_card["records"] == live["records"]


def _history_service_facts():
    from copy import deepcopy
    from uuid import UUID

    from tests.test_f2_service_evidence import ServiceFacts

    class PublishedFacts(ServiceFacts):
        def query(self, *, actor, payload):
            result = super().query(actor=actor, payload=payload)
            result.update(
                queried_at="2026-09-12T01:00:00+00:00",
                factual_at="2026-09-12T01:00:00+00:00",
                query=payload.model_dump(mode="json"),
                returned_count=len(result["records"]),
                next_cursor=None,
            )
            result["coverage"]["description"] = (
                "当前履职身份明确获权的事实对象及请求时点；不含隐含下级范围，空结果不证明现实不存在。"
            )
            result.setdefault(
                "quality", {"evidence_status": "known", "source_availability": "unknown"}
            )
            for row in result["records"]:
                row.update(
                    fact_kind=result["fact_kind"],
                    version=1,
                    valid_from="2026-09-01T00:00:00+00:00",
                    valid_to=None,
                    source={
                        "kind": "formal_publication",
                        "label": "学校正式发布资料",
                        "reference": "publication:" + str(UUID(int=101)),
                        "published_at": "2026-09-01T09:30:00+00:00",
                    },
                )
            return result

    facts = PublishedFacts()
    service = facts.rows[("school_service", None)]["records"][0]
    service["fields"].update(audience_description="校内教职工", service_summary="资料登记与问询")
    second = deepcopy(service)
    second["reference"] = "f2:school_service:service002"
    second["fields"].update(display_name="现场咨询", service_summary="当面登记与资料核对")
    second["fields"].pop("official_contact_ref")
    facts.rows[("school_service", None)]["records"].append(second)
    facts.rows[("school_service", None)]["matched_count"] = 2
    claims = []
    for index, (kind, detail) in enumerate(
        [
            ("source", {"source_status": "unavailable", "responsibility": "资料办公室"}),
            ("coverage", {"coverage_status": "partial"}),
            ("dispute", {"dispute_status": "open", "evidence_reference": "dispute:record-23"}),
            ("review", {"review_due_at": "2026-09-05T00:00:00+00:00"}),
        ],
        start=1,
    ):
        claims.append(
            {
                "reference": "f2:fact_quality:" + str(UUID(int=200 + index)),
                "kind": kind,
                "owner_kind": "organization",
                "owner_ref": "f2:organization:office0000",
                "target_fact_kind": "school_service",
                "target_ref": None,
                "applies_to_query": True,
                "applies_to_records": [service["reference"], second["reference"]],
                "valid_from": "2026-09-01T00:00:00+00:00",
                "valid_to": None,
                "basis_reference": "校办〔2026〕18号",
                **detail,
            }
        )
    facts.rows[("school_service", None)]["quality"] = {
        "evidence_status": "known",
        "source_availability": "unavailable",
        "coverage": "partial",
        "conflict": "open",
        "freshness": "review_overdue",
        "existence": "known",
        "statements": claims,
    }
    references = {
        row["reference"]: "f2:" + kind + ":" + str(UUID(int=index * 10 + position))
        for index, ((kind, _), result) in enumerate(facts.rows.items(), start=1)
        for position, row in enumerate(result["records"], start=1)
    }

    def replace_refs(value):
        if isinstance(value, dict):
            return {key: replace_refs(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_refs(item) for item in value]
        return references.get(value, value) if isinstance(value, str) else value

    facts.rows = {
        (kind, references.get(reference, reference)): replace_refs(result)
        for (kind, reference), result in facts.rows.items()
    }
    return facts


def test_default_five_family_service_history_survives_actual_next_turn():
    import json
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE
    from app.platform.assistant.runtime import AssistantRuntime
    from app.platform.assistant.tool_loop import AssistantToolLoop, ModelReply, ModelToolCall
    from tests.assistant.test_tool_loop import ScriptModel

    facts = _history_service_facts()
    gateway, _, _ = _directory_read_turn(facts)
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("private-actor", uuid4())
    accepted = runtime.accept_turn(
        conversation.conversation_ref,
        "private-actor",
        uuid4(),
        "两项服务的渠道与提供组织在哪个校区？",
    )
    model = ScriptModel(
        [
            ModelReply(
                calls=(
                    ModelToolCall(
                        call_id="services",
                        name=MODULE + ".lookup",
                        arguments={
                            "subject_kind": "school_service",
                            "scope": "all_authorized",
                            "include": ["official_contact", "organization_campus_relationship"],
                        },
                    ),
                )
            ),
            ModelReply(
                text="已取得两项服务和提供组织的校区关联；第二项渠道未登记引用，来源、覆盖、争议和核实期限仍有风险。"
            ),
        ]
    )
    snapshot = AssistantToolLoop(
        model=cast(ToolModel, model), gateway_factory=gateway.for_turn
    ).run_accepted(
        runtime=runtime, accepted=accepted, actor_context_ref="private-actor", request_id=uuid4()
    )
    note = snapshot.model_safe_ledger[-1].model_safe_note
    assert note is not None
    assert len(note) <= 8_000
    history = json.loads(note)[0]
    assert sum(len(view["records"]) for view in history["results"]) == 6
    assert history["service_results"][1]["official_contact"]["outcome"] == (
        "获准结果未提供关联引用，不能据此认定未设置或不存在"
    )
    assert len(_restored_history_statements(
        history["results"][0]["supporting_evidence"]["readable_quality"]
    )) == 4
    follow = runtime.accept_turn(
        conversation.conversation_ref, "private-actor", uuid4(), "发布来源和限制呢？"
    )
    second = ScriptModel([ModelReply(text="可根据刚才的发布来源与风险声明说明。")])
    AssistantToolLoop(model=cast(ToolModel, second), gateway_factory=gateway.for_turn).run_accepted(
        runtime=runtime, accepted=follow, actor_context_ref="private-actor", request_id=uuid4()
    )
    quoted = json.loads(second.requests[0].messages[1].content)["turns"][-1]
    assert quoted["quoted_tool_results"] == [history]
    assert "advice@example.invalid" in str(quoted)
    assert all(
        value in str(quoted)
        for value in (
            "学校正式发布资料",
            "资料咨询",
            "现场咨询",
            "来源不可用",
            "部分覆盖",
            "争议",
            "核实期限",
        )
    )


def _restored_history_statements(quality):
    from copy import deepcopy

    restored = deepcopy(quality.get("statements", []))
    for group in quality.get("statement_groups", []):
        shared = {key: value for key, value in group.items() if key != "statements"}
        for statement in group["statements"]:
            claim = {**deepcopy(shared), **deepcopy(statement)}
            claim["tool_context"] = {
                **deepcopy(shared.get("tool_context", {})),
                **deepcopy(statement.get("tool_context", {})),
            }
            restored.append(claim)
    return sorted(restored, key=lambda claim: claim["tool_context"]["reference"])


@pytest.mark.parametrize(
    "changed_scope",
    [
        {"purpose": "office", "contact_kind": "phone"},
        {"valid_to": "2026-09-20T00:00:00+00:00"},
        {"target_ref": "f2:school_service:another-target"},
        {"owner_ref": "f2:organization:another-owner", "applies_to_query": False},
    ],
)
def test_history_shares_only_identical_sources_and_statement_scope(changed_scope):
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import (
        directory_history_material,
        directory_model_material,
        directory_model_projection,
    )
    from app.composition.school_fact_directory_lookup import FactLookupInput, lookup
    from tests.test_f2_directory_product import actor

    raw = lookup(
        _history_service_facts(),
        actor=actor(),
        payload=FactLookupInput(
            subject_kind="school_service",
            scope="all_authorized",
            include=("official_contact", "organization_campus_relationship"),
        ),
    )
    extra = deepcopy(raw["results"][0]["quality"]["statements"][0])
    extra.update(reference="f2:fact_quality:distinct", **changed_scope)
    raw["results"][0]["quality"]["statements"].append(extra)
    raw["results"][-1]["records"][0]["source"]["published_at"] = "2026-09-02T00:00:00+00:00"
    before = deepcopy(raw)
    current = directory_model_material(directory_model_projection(raw))
    from app.composition.school_fact_directory_gateway import _deduplicate_service_facts

    _deduplicate_service_facts(current, restore=True)
    history = directory_history_material(directory_model_projection(raw))
    assert "publication_sources" in history
    for old_view, new_view in zip(current["results"], history["results"], strict=True):
        for old, new in zip(old_view["records"], new_view["records"], strict=True):
            source = (
                history["publication_sources"][new["publication_source_index"]]
                if "publication_source_index" in new
                else new["source"]
            )
            assert source == old["source"]
            assert new["readable_facts"] == old["readable_facts"]
            assert new.get("relationship_facts") == old.get("relationship_facts")
        for key, value in old_view["authorized_result_set"].items():
            if key == "next_cursor" and value is None:
                continue
            assert (
                new_view["authorized_result_set"].get(
                    key, history.get("result_set_semantics", {}).get(key)
                )
                == value
            )
    quality = history["results"][0]["readable_quality"]
    assert "statement_groups" in quality
    expected = deepcopy(current["results"][0]["readable_quality"]["statements"])
    for claim in expected:
        claim.pop("supporting_evidence", None)
    assert _restored_history_statements(quality) == sorted(
        expected, key=lambda claim: claim["tool_context"]["reference"]
    )
    assert raw == before


@pytest.mark.parametrize(
    "difference", ["none", "fact_type", "factual_at", "valid_from", "valid_to", "missing_valid_to"]
)
def test_history_inherits_only_equal_fact_types_and_times(difference):
    from copy import deepcopy

    from app.composition.school_fact_directory_gateway import (
        directory_history_material,
        directory_model_material,
        directory_model_projection,
    )
    from app.composition.school_fact_directory_lookup import FactLookupInput, lookup
    from tests.test_f2_directory_product import actor

    raw = lookup(
        _history_service_facts(),
        actor=actor(),
        payload=FactLookupInput(
            subject_kind="school_service",
            scope="all_authorized",
            include=("official_contact", "organization_campus_relationship"),
        ),
    )
    changed = raw["results"][0]["records"][0]
    if difference == "fact_type":
        changed["fact_kind"] = "organization"
        changed["reference"] = changed["reference"].replace(":school_service:", ":organization:")
    elif difference == "factual_at":
        raw["results"][0]["factual_at"] = "2026-09-11T01:00:00+00:00"
    elif difference == "valid_from":
        changed["valid_from"] = "2026-09-02T00:00:00+00:00"
    elif difference == "valid_to":
        changed["valid_to"] = "2026-09-20T00:00:00+00:00"
    elif difference == "missing_valid_to":
        changed.pop("valid_to")
    before = deepcopy(raw)
    current = directory_model_material(directory_model_projection(raw))
    from app.composition.school_fact_directory_gateway import _deduplicate_service_facts

    _deduplicate_service_facts(current, restore=True)
    history = directory_history_material(directory_model_projection(raw))
    has_shared_period = difference != "missing_valid_to"
    if has_shared_period:
        assert history["record_validity"] == {
            "valid_from": "2026-09-01T00:00:00+00:00",
            "valid_to": None,
        }
        assert "record_validity" in history["history_shared_fields"]
    else:
        assert "record_validity" not in history
    if difference != "factual_at":
        assert history["factual_at"] == current["results"][0]["factual_at"]
        assert "factual_at" in history["history_shared_fields"]
    else:
        assert "factual_at" not in history
    assert "fact_type" in history["history_shared_fields"]
    for old_view, new_view in zip(current["results"], history["results"], strict=True):
        assert new_view["fact_type"] == old_view["fact_type"]
        if difference != "factual_at":
            assert "factual_at" not in new_view
        assert new_view.get("factual_at", history.get("factual_at")) == old_view["factual_at"]
        for old, new in zip(old_view["records"], new_view["records"], strict=True):
            assert new.get("fact_type", new_view["fact_type"]) == old["fact_type"]
            assert ("fact_type" not in new) == (old["fact_type"] == old_view["fact_type"])
            old_kind = old["tool_context"]["fact_kind"]
            view_kind = new_view["tool_context"]["fact_kind"]
            assert new["tool_context"].get("fact_kind", view_kind) == old_kind
            assert ("fact_kind" not in new["tool_context"]) == (old_kind == view_kind)
            inherited_period = has_shared_period and all(
                old.get(key) == history["record_validity"][key]
                for key in ("valid_from", "valid_to")
            )
            for key in ("valid_from", "valid_to"):
                if inherited_period:
                    assert key not in new
                    assert history["record_validity"][key] == old[key]
                else:
                    assert (key in new) == (key in old)
                    assert new.get(key) == old.get(key)
            assert new["readable_facts"] == old["readable_facts"]
            assert new.get("relationship_facts") == old.get("relationship_facts")
    assert raw == before


def test_directory_query_declares_education_boundary_and_preserves_generic_reads():
    from app.composition.school_fact_directory_gateway import DirectoryFactToolGateway
    from app.composition.school_fact_directory_product import FactQueryInput
    from app.platform.authorization.f2_reader_compiler import F2_DEFINITIONS

    query = next(
        tool for tool in DirectoryFactToolGateway.descriptors if tool.name.endswith(".query")
    )
    assert "organization表示通用学校组织" in query.description
    assert "assignment表示人员在组织中的正式任职" in query.description
    assert "本目录工具不查询学年、年级、行政班及班主任对应关系" in query.description
    assert "查询结果不能替代这些教育关系" in query.description
    assert FactQueryInput(fact_kind="organization", query_text="科研组").query_text == "科研组"
    assert FactQueryInput(fact_kind="assignment", reference="f2:assignment:sample").reference
    kind_ref = query.parameters["properties"]["fact_kind"]["$ref"].rsplit("/", 1)[-1]
    assert set(query.parameters["$defs"][kind_ref]["enum"]) == set(F2_DEFINITIONS)


def test_directory_pages_cover_native_page_without_skips_and_reject_mutation():
    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_directory_product import FactQueryInput
    from app.composition.school_fact_f2_query_repository import F2FactQueryResult, F2PublicFact
    from app.composition.school_fact_product_contacts import ContactProductUnavailable
    from tests.test_f2_directory_product import NOW, actor

    rows = tuple(
        F2PublicFact(
            f"f2:person:{i:064x}",
            "person",
            {"display_name": str(i)},
            1,
            NOW,
            None,
            "formal_publication",
            "publication:test",
            NOW,
        )
        for i in range(205)
    )
    revision = [1]
    calls = []

    class Repository:
        def query(self, **kw):
            calls.append(kw)
            if kw["fact_kind"] == "fact_quality":
                return F2FactQueryResult("no_current_records", (), 0, False, None, NOW)
            start = int(kw.get("cursor") or 0)
            selected = (
                rows
                if kw.get("name") is None
                else tuple(r for r in rows if r.content["display_name"] == kw["name"])
            )
            page = selected[start : start + 100]
            return F2FactQueryResult(
                "known",
                page,
                len(selected),
                start + 100 < len(selected),
                str(start + 100) if start + 100 < len(selected) else None,
                NOW,
                factual_at=NOW,
                read_version=revision[0],
            )

    adapter = DirectoryProductAdapter(Repository())
    query = FactQueryInput(fact_kind="person", scope="all_authorized", page_size=20)
    first = adapter.query(actor=actor(), payload=query)
    assert 8 < len(first["records"]) <= 20
    assert first["next_cursor"] and first["next_cursor"] != "100"
    seen = list(first["records"])
    current = first
    while current["next_cursor"]:
        current = adapter.query(
            actor=actor(), payload=query.model_copy(update={"cursor": current["next_cursor"]})
        )
        assert current["matched_count"] == 205
        directory_model_projection(current)
        seen.extend(current["records"])
    assert [r["reference"] for r in seen] == [r.fact_ref for r in rows]
    assert len({r["reference"] for r in seen}) == 205
    with pytest.raises(ContactProductUnavailable):
        adapter.query(
            actor=actor(), payload=query.model_copy(update={"cursor": first["next_cursor"] + "x"})
        )
    with pytest.raises(ContactProductUnavailable):
        adapter.query(
            actor=actor(),
            payload=query.model_copy(
                update={"cursor": first["next_cursor"], "as_of": NOW.replace(year=2025)}
            ),
        )
    revision[0] = 2
    with pytest.raises(ContactProductUnavailable):
        adapter.query(
            actor=actor(), payload=query.model_copy(update={"cursor": first["next_cursor"]})
        )
    exact = adapter.query(
        actor=actor(), payload=FactQueryInput(fact_kind="person", query_text="204")
    )
    assert [r["label"] for r in exact["records"]] == ["204"]


def test_directory_policy_pages_retain_every_excerpt_card_and_tool_handle(monkeypatch):
    from types import SimpleNamespace
    from uuid import UUID

    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_directory_gateway import MODULE, DirectoryFactToolGateway
    from app.composition.school_fact_directory_product import FactQueryInput
    from app.composition.school_fact_f2_query_repository import F2FactQueryRepository
    from app.composition.school_fact_product_contacts import ContactProductUnavailable
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from app.platform.authorization.f2_reader_contracts import F2FactRow
    from app.platform.authorization.secure_reader_contracts import SecureReadDenied, SecureReadPage
    from tests.test_f2_directory_product import NOW, actor, setup

    live = [True]
    rows = tuple(
        F2FactRow(
            UUID(int=i + 1),
            "policy_version",
            f"policy.{i}",
            {"display_name": str(i), "content_text": "甲乙丙丁" * 5000},
            1,
            4,
            NOW,
            None,
            "formal_publication",
            UUID(int=90 + i),
            NOW,
        )
        for i in range(20)
    )

    class Reader:
        def read(self, request):
            if not live[0]:
                raise SecureReadDenied("revoked")
            if request.operation_code == "school_facts.f2.fact_quality.read":
                return SecureReadPage((), 0, NOW, None)
            return SecureReadPage(rows, len(rows), request.factual_at or NOW, None)

    repository = F2FactQueryRepository(
        reader=Reader(),
        quality_readers={"policy_version": Reader()},
        reference_key=b"synthetic-test-only",
        clock=lambda: NOW,
    )
    adapter = DirectoryProductAdapter(repository)
    service, _, _, _ = setup()
    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=actor(), session_binding="session"),
        query=adapter,
        product=service,
        retention_seconds=60,
    )
    context = ToolCallContext("private", "conversation", "turn", "run", 1, UUID(int=99))
    arguments = {"fact_kind": "policy_version", "scope": "all_authorized"}
    seen = []
    first_cursor = None
    for index in range(25):
        outcome = gateway.for_turn("private").execute(
            ModelToolCall(call_id=str(index), name=MODULE + ".query", arguments=arguments), context
        )
        assert outcome.fact_card and outcome.fact_card["records"]
        page = outcome.fact_card
        assert len(page["records"]) == len(outcome.model_content["records"])
        assert outcome.model_content["authorized_result_set"]["next_cursor"] == page["next_cursor"]
        assert page["matched_count"] == 20
        assert outcome.history_check is not None and outcome.history_check.valid()
        seen.extend(r["reference"] for r in page["records"])
        if not page["next_cursor"]:
            break
        first_cursor = first_cursor or page["next_cursor"]
        arguments = {**arguments, "cursor": page["next_cursor"]}
    assert len(seen) == len(set(seen)) == 20
    query = FactQueryInput(fact_kind="policy_version", scope="all_authorized", cursor=first_cursor)
    live[0] = False
    with pytest.raises(ContactProductUnavailable):
        adapter.query(actor=actor(), payload=query)
    live[0] = True
    from app.platform import contracts as actors

    other = actors._issue_staff_actor_context(
        account_user_id=8,
        school_id=actor().school_id,
        person_id=actor().person_id,
        assignment_id=actor().assignment_id,
        assignment_version=1,
        authorization_version=1,
    )
    with pytest.raises(ContactProductUnavailable):
        adapter.query(actor=other, payload=query)
    monkeypatch.setattr(
        "app.composition.school_fact_directory_adapter.monotonic", lambda: float("inf")
    )
    with pytest.raises(ContactProductUnavailable):
        adapter.query(actor=actor(), payload=query)


def test_expired_directory_page_explicitly_requests_restart_without_returning_rows():
    from types import SimpleNamespace
    from uuid import UUID

    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_directory_gateway import MODULE, DirectoryFactToolGateway
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from tests.test_f2_directory_product import actor, setup

    service, _, _, _ = setup()
    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=actor(), session_binding="session"),
        query=DirectoryProductAdapter(SimpleNamespace()),
        product=service,
        retention_seconds=60,
    )
    result = gateway.execute(
        ModelToolCall(
            call_id="expired",
            name=MODULE + ".query",
            arguments={"fact_kind": "person", "scope": "all_authorized", "cursor": "expired-page"},
        ),
        ToolCallContext("private", "conversation", "turn", "run", 1, UUID(int=99)),
    )
    assert result.fact_card is None
    assert result.model_content["restart_required"] is True
    assert "重新查询" in result.model_content["reason"]


@pytest.mark.parametrize("consumer", ["family", "education"])
def test_internal_directory_reference_choices_keep_original_bounded_page(consumer):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_education_views import EducationMaintenanceViews
    from app.composition.school_fact_f2_query_repository import F2FactQueryResult, F2PublicFact
    from app.composition.school_fact_family_services import DirectoryFamilyReferences
    from tests.test_f2_directory_product import NOW, actor

    rows = tuple(
        F2PublicFact(
            f"f2:location:{i:064x}",
            "location",
            {"display_name": "长名称" * 100 + str(i)},
            1,
            NOW,
            None,
            "formal_publication",
            "publication:test",
            NOW,
        )
        for i in range(30)
    )
    calls = []

    class Repository:
        def query(self, **kw):
            calls.append(kw)
            if kw["fact_kind"] == "fact_quality":
                return F2FactQueryResult("no_current_records", (), 0, False, None, NOW)
            return F2FactQueryResult(
                "known", rows, 30, False, None, NOW, factual_at=NOW, read_version=1
            )

    class Evidence:
        @contextmanager
        def capture(self, **kw):
            yield SimpleNamespace(seal=lambda: SimpleNamespace(check=lambda who: True))

    repository = Repository()
    if consumer == "family":
        result = DirectoryFamilyReferences(repository, evidence=Evidence()).choices(
            actor=actor(), fact_kind="location"
        )
    else:
        views = EducationMaintenanceViews(
            SimpleNamespace(repository=None),
            directory=DirectoryProductAdapter(repository),
            family=None,
        )
        result = views.query(actor=actor(), fact_kind="location")
    assert len(result["records"]) == result["matched_count"] == result["returned_count"] == 30
    assert result["records"][-1]["label"] == "长名称" * 100 + "29"
    assert result["next_cursor"] is None and result["truncated"] is False
    assert calls[0]["page_size"] == 100
