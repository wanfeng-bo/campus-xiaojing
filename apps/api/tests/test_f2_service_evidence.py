"""Service lookup dependencies must not survive changed facts or identity."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.composition.school_fact_directory_gateway import MODULE, DirectoryFactToolGateway
from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
from tests.test_f2_directory_lookup import record, view
from tests.test_f2_directory_product import actor
from tests.test_f2_read_evidence import NOW, Backend


class ServiceFacts:
    def __init__(self):
        self.rows = {
            ("school_service", None): view(
                "school_service",
                record(
                    "f2:school_service:service000",
                    "资料咨询",
                    display_name="资料咨询",
                    organization_ref="f2:organization:office0000",
                    official_contact_ref="f2:official_contact:contact000",
                ),
            ),
            ("official_contact", "f2:official_contact:contact000"): view(
                "official_contact",
                record(
                    "f2:official_contact:contact000",
                    "办公室邮箱",
                    contact_value="advice@example.invalid",
                ),
            ),
            ("organization", "f2:organization:office0000"): view(
                "organization",
                record("f2:organization:office0000", "服务办公室", display_name="服务办公室"),
            ),
            ("organization_campus_relationship", "f2:organization:office0000"): view(
                "organization_campus_relationship",
                record(
                    "f2:organization_campus_relationship:link0000",
                    "组织校区关系",
                    organization_ref="f2:organization:office0000",
                    campus_ref="f2:location:campus0000",
                ),
            ),
            ("location", "f2:location:campus0000"): view(
                "location",
                record(
                    "f2:location:campus0000", "青石校区", display_name="青石校区", kind="campus"
                ),
            ),
        }
        self.calls = []

    def query(self, *, actor: object, payload: object):
        self.calls.append(payload)
        key = payload.fact_kind, payload.reference or payload.query_text or payload.related_ref
        return deepcopy(self.rows[key])


def started(facts=None, *, as_of=None):
    facts = facts or ServiceFacts()
    live = {"active": True}
    identity = SimpleNamespace(actor=actor(), session_binding="one-session")

    def resolve(ref):
        if not live["active"]:
            raise AssistantRuntimeError("session ended")
        assert ref == "private-actor"
        return identity

    gateway = DirectoryFactToolGateway(
        resolve_actor=resolve, query=facts, product=None, retention_seconds=60
    )
    turn = gateway.for_turn("private-actor")
    result = turn.execute(
        ModelToolCall(
            call_id="service",
            name=MODULE + ".lookup",
            arguments={
                "subject_kind": "school_service",
                "scope": "all_authorized",
                "include": ["official_contact", "organization_campus_relationship"],
                **({"as_of": as_of.isoformat()} if as_of is not None else {}),
            },
        ),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    return facts, live, turn, result


@pytest.mark.parametrize(
    "change",
    [
        "membership_add",
        "membership_remove",
        "channel",
        "link",
        "quality",
        "channel_denied",
        "logout",
    ],
)
def test_service_fallback_dependencies_changed_before_reuse_are_rejected(change):
    facts, live, turn, result = started()
    assert result.history_check is not None and result.history_check.valid()
    turn.revalidate("private-actor")
    if change == "membership_add":
        extra = deepcopy(facts.rows[("school_service", None)]["records"][0])
        extra["reference"] = "f2:school_service:second0000"
        facts.rows[("school_service", None)]["records"].append(extra)
    elif change == "membership_remove":
        facts.rows[("school_service", None)] = view("school_service")
    elif change == "channel":
        facts.rows[("official_contact", "f2:official_contact:contact000")]["records"][0]["fields"][
            "contact_value"
        ] = "changed@example.invalid"
    elif change == "link":
        facts.rows[("organization_campus_relationship", "f2:organization:office0000")] = view(
            "organization_campus_relationship"
        )
    elif change == "quality":
        facts.rows[("school_service", None)]["quality"] = {
            "source_availability": "unavailable",
            "evidence_status": "known",
        }
    elif change == "channel_denied":
        facts.rows[("official_contact", "f2:official_contact:contact000")] = view(
            "official_contact", status="denied"
        )
    else:
        live["active"] = False
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("private-actor")


def test_service_fallback_does_not_reuse_a_denied_contact_result():
    facts, live, turn, result = started()
    facts.rows[("official_contact", "f2:official_contact:contact000")] = view(
        "official_contact", status="denied"
    )
    # Old history is invalid, not reused to fill the denied channel.
    assert not result.history_check.valid()
    assert any(
        call.fact_kind == "official_contact" and call.reference == "f2:official_contact:contact000"
        for call in facts.calls
    )


class MechanicalServiceCapture(Backend):
    """Real ReadCapture/ReadEvidence, controlled port metadata; not a SQL/RLS test.

    The existing Backend provides a controllable clock and authority version.
    Every adapter read records its exact selection; probes contain membership
    metadata only. This tests lookup-to-capture wiring independently of fallback.
    """

    def __init__(self, facts):
        super().__init__()
        self.facts = facts
        self.active_capture = None
        self.captures = []
        self.projection = "original-fields"

    @staticmethod
    def decision_key(request):
        return str(request)

    @staticmethod
    def metadata_key(probe):
        return probe.key

    def decision(self, request):
        self.calls.append("decision")
        return self.authority + ":" + self.projection

    @contextmanager
    def capture(self, *, actor, current_time):
        from app.composition.school_fact_read_capture import ReadCapture

        capture = ReadCapture(self, actor, current_time)
        self.captures.append(capture)
        self.active_capture = capture
        try:
            yield capture
        finally:
            self.active_capture = None

    def metadata(self, probe):
        self.calls.append("metadata")
        when = self.now if probe.current_time else probe.arguments["as_of"]
        return self.facts.metadata_at(tuple(probe.arguments["selection"]), when)


class TimedServiceFacts(ServiceFacts):
    """Controlled formal-query port, with explicit availability windows.

    This intentionally does not claim to execute the production SQL compiler.
    SQL temporal predicates retain their separate reader verification evidence.
    """

    def __init__(self, *, capture):
        super().__init__()
        self.clock_backend = MechanicalServiceCapture(self)
        if capture:
            self.evidence = self.clock_backend
        self.windows = {}
        self.hide_service_reference = False

    def result_at(self, key, when):
        result = deepcopy(self.rows[key])
        start, end = self.windows.get(key, (None, None))
        if start is not None and when < start or end is not None and when >= end:
            return view(key[0])
        if self.hide_service_reference and key[0] == "school_service":
            for item in result["records"]:
                item["fields"].pop("official_contact_ref", None)
        return result

    def metadata_at(self, key, when):
        result = self.result_at(key, when)
        # Only set membership/version, never body text in proof metadata.
        return len(result["records"]), tuple((item["reference"], 1) for item in result["records"])

    def query(self, *, actor, payload):
        self.calls.append(payload)
        key = payload.fact_kind, payload.reference or payload.query_text or payload.related_ref
        when = payload.as_of or self.clock_backend.now
        result = self.result_at(key, when)
        capture = self.clock_backend.active_capture
        if capture is not None:
            # Exercise real capture registration/sealing. The query/decision
            # providers here are explicit test ports, not replacement production code.
            relation = object()
            arguments = {"selection": key, "as_of": when}
            capture.relations[id(relation)] = (relation, arguments, capture.current_time)
            capture.record_decision(key, self.clock_backend.decision(key))
            total, metadata = self.metadata_at(key, when)
            capture.record_page(relation, total, metadata, [])
        return result


@pytest.mark.parametrize("use_capture", [False, True], ids=["fallback", "capture"])
def test_service_empty_selection_then_member_added_invalidates_exact_result(use_capture):
    facts = TimedServiceFacts(capture=use_capture)
    original = deepcopy(facts.rows[("school_service", None)])
    facts.rows[("school_service", None)] = view("school_service")
    facts, _, turn, result = started(facts)
    assert result.fact_card["service_groups"] == []
    assert result.history_check.valid()
    if use_capture:
        assert facts.clock_backend.captures[0].probes
        assert facts.clock_backend.captures[0].seal() is not None
    facts.rows[("school_service", None)] = original
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("private-actor")


@pytest.mark.parametrize("use_capture", [False, True], ids=["fallback", "capture"])
def test_service_hidden_reference_invalidates_history_without_claiming_unset(use_capture):
    facts, _, turn, result = started(TimedServiceFacts(capture=use_capture))
    assert result.history_check.valid()
    facts.hide_service_reference = True
    facts.clock_backend.projection = "reference-not-in-projection"
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("private-actor")
    facts.calls.clear()
    _, _, _, new_result = started(facts)
    item = new_result.fact_card["service_groups"][0]["official_contact"]
    assert item == {"status": "reference_not_provided"}
    assert new_result.fact_card["query_completion"] == "completed"
    assert new_result.fact_card["status"] == "partial"
    assert not any(call.fact_kind == "official_contact" for call in facts.calls)
    assert "not_set" not in str(item) and "no_current_records" not in str(item)


@pytest.mark.parametrize("use_capture", [False, True], ids=["fallback", "capture"])
@pytest.mark.parametrize("transition", ["service_begins", "channel_ends"])
def test_service_current_result_crossing_validity_boundary_is_not_reused(use_capture, transition):
    facts = TimedServiceFacts(capture=use_capture)
    boundary = NOW + timedelta(seconds=5)
    if transition == "service_begins":
        facts.windows[("school_service", None)] = (boundary, None)
    else:
        facts.windows[("official_contact", "f2:official_contact:contact000")] = (None, boundary)
    facts, _, turn, result = started(facts)
    assert result.history_check.valid()
    if use_capture:
        assert all(probe.current_time for probe, _ in facts.clock_backend.captures[0].probes)
        count = len(facts.calls)
        assert result.history_check.valid()
        assert len(facts.calls) == count  # Real proof path, no full-query fallback.
    facts.clock_backend.now = boundary
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("private-actor")


def test_service_historical_probes_keep_selected_time_without_freezing_current_authority():
    facts = TimedServiceFacts(capture=True)
    facts.windows[("official_contact", "f2:official_contact:contact000")] = (
        None,
        NOW + timedelta(seconds=5),
    )
    facts, _, turn, result = started(facts, as_of=NOW)
    assert result.history_check.valid()
    assert all(not probe.current_time for probe, _ in facts.clock_backend.captures[0].probes)
    facts.clock_backend.now = NOW + timedelta(seconds=10)
    assert result.history_check.valid()
    facts.clock_backend.authority = "denied-now"
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("private-actor")


@pytest.mark.parametrize("change", ["second_name", "provider", "quality_permission", "time"])
def test_selected_targets_share_one_capture_and_keep_all_history_dependencies(change):
    from app.composition.school_fact_directory_gateway import DirectoryFactToolGateway

    facts = TimedServiceFacts(capture=True)
    first = deepcopy(facts.rows[("school_service", None)]["records"][0])
    second = deepcopy(first)
    second["reference"] = "f2:school_service:second0000"
    second["fields"].pop("official_contact_ref")
    facts.rows[("school_service", "服务甲")] = view("school_service", first)
    facts.rows[("school_service", "服务乙")] = view("school_service", second)
    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=actor(), session_binding="one-session"),
        query=facts,
        product=None,
        retention_seconds=60,
    )
    turn = gateway.for_turn("private-actor")
    result = turn.execute(
        ModelToolCall(
            call_id="targets",
            name=MODULE + ".lookup",
            arguments={
                "subject_kind": "school_service",
                "targets": [{"query_text": "服务甲"}, {"query_text": "服务乙"}],
                "include": ["official_contact", "organization_campus_relationship"],
            },
        ),
        ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4()),
    )
    assert len(facts.calls) == 6
    assert len(facts.clock_backend.captures) == 1
    assert len(facts.clock_backend.captures[0].probes) == 6
    assert result.history_check.valid()
    assert len(facts.calls) == 6
    if change == "second_name":
        facts.rows[("school_service", "服务乙")] = view("school_service")
    elif change == "provider":
        facts.rows[("organization", "f2:organization:office0000")] = view("organization")
    elif change == "quality_permission":
        facts.clock_backend.authority = "revoked"
    else:
        boundary = NOW + timedelta(seconds=5)
        facts.windows[("school_service", "服务乙")] = (None, boundary)
        facts.clock_backend.now = boundary
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("private-actor")
