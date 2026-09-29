"""Read-only own-receipt recovery; no model calls or school mutations."""

from dataclasses import replace
from datetime import timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from app.composition.school_fact_directory_adapter import FACT_LABELS
from app.composition.school_fact_directory_product import (
    DirectoryFactProductService,
    OwnReceiptTarget,
)
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.composition.school_fact_product import shared_own_receipt_projection
from app.composition.school_fact_product_contacts import ContactProductUnavailable
from app.composition.school_facts import InMemorySchoolFactPlanStore
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.change_boundary import SchoolFactChangeBoundary
from app.platform.school_facts.ports import (
    SchoolFactApplyResult,
    SchoolFactOwnResult,
    SchoolFactOwnResultPage,
    SchoolFactOwnResultTarget,
    SchoolFactOwnTargetPage,
    SchoolFactOwnTerminal,
    SchoolFactResultLookup,
)
from tests.test_f2_directory_http import BASE, client_for
from tests.test_f2_directory_product import NOW, Boundary, actor, real_directory_adapter, setup

LOCATOR = "a" * 64
OTHER = "b" * 64
PRIVATE_SOURCE = "private-original-object-key"


def own_result(locator=LOCATOR):
    return SchoolFactOwnResult(
        locator_sha256=locator,
        confirmed_at=NOW - timedelta(days=1),
        result=SchoolFactApplyResult(UUID(int=91), 13, 1, False),
        targets=(SchoolFactOwnResultTarget("official_contact", PRIVATE_SOURCE, "upsert", 3),),
    )


class OwnBoundary(Boundary):
    def __init__(self):
        super().__init__()
        self.items = (own_result(), own_result(OTHER))
        self.selected: SchoolFactOwnResult | None = self.items[0]
        self.error: Exception | None = None

    def list_own_results(self, **kwargs):
        self.calls.append(("own_list", kwargs))
        if self.error is not None:
            raise self.error
        return SchoolFactOwnResultPage(
            self.items[: kwargs["limit"]], len(self.items) > kwargs["limit"]
        )

    def check_own_result_by_locator(self, **kwargs):
        self.calls.append(("own_detail", kwargs))
        if self.error is not None:
            raise self.error
        return self.selected

    def check_result_by_locator(self, **kwargs):
        self.calls.append(("old_receipt", kwargs))
        return SchoolFactResultLookup(
            found=kwargs["session_binding"] == "old-session",
            result=self.result if kwargs["session_binding"] == "old-session" else None,
        )


def own_service():
    existing, _, _, _ = setup()
    boundary = OwnBoundary()
    adapter, _ = real_directory_adapter("official_contact", {})
    service = DirectoryFactProductService(
        boundary=cast(SchoolFactChangeBoundary, boundary),
        resolve_change=existing._resolve,
        require_maintenance=existing._require,
        require_receipt=existing._require_receipt,
        plan_store=InMemorySchoolFactPlanStore(clock=lambda: NOW),
        receipt_reference_for=adapter.repository.reference_for,
        receipt_fact_labels=FACT_LABELS,
        clock=lambda: NOW,
    )
    return service, boundary, adapter


def detail(service, reference="sf2receipt_" + LOCATOR):
    return service.own_receipt(
        actor=actor(), session_binding="new-session", request_id=uuid4(), receipt_ref=reference
    )


def test_large_original_receipt_returns_only_currently_authorized_target_page():
    service, boundary, _ = own_service()
    boundary.selected = None
    terminal = SchoolFactOwnTerminal(
        LOCATOR, NOW - timedelta(days=1),
        SchoolFactApplyResult(UUID(int=91), 13, 4097, True), 4097,
    )
    page = SchoolFactOwnTargetPage(
        terminal,
        (SchoolFactOwnResultTarget("official_contact", PRIVATE_SOURCE, "upsert", 3),),
        4096,
        4097,
        False,
    )

    def read_page(**kwargs):
        boundary.calls.append(("large-page", kwargs))
        return page

    boundary.read_own_target_page = read_page
    receipt = service.own_receipt(
        actor=actor(), session_binding="new-session", request_id=uuid4(),
        receipt_ref="sf2receipt_" + LOCATOR, target_offset=4096, target_limit=20,
    )
    assert receipt.status == "succeeded" and receipt.changed_count == 4097
    assert receipt.targets[0].reference.startswith("f2:official_contact:")
    assert receipt.target_page is not None
    assert (receipt.target_page.offset, receipt.target_page.next_offset) == (4096, 4097)
    assert receipt.target_page.target_count == 4097
    assert receipt.target_page.has_more is False
    assert PRIVATE_SOURCE not in receipt.model_dump_json()
    assert boundary.calls[-1][0] == "large-page"


def test_current_target_denial_can_still_return_independently_authorized_terminal():
    service, boundary, _ = own_service()
    boundary.error = SchoolFactChangeRejected("old target rights expired")
    terminal = SchoolFactOwnTerminal(
        LOCATOR,
        NOW - timedelta(days=1),
        SchoolFactApplyResult(UUID(int=91), 13, 1, True),
        1,
    )

    def read_page(**kwargs):
        boundary.calls.append(("independent-page", kwargs))
        return SchoolFactOwnTargetPage(terminal, (), 0, 1, False)

    boundary.read_own_target_page = read_page
    receipt = detail(service)
    assert receipt.status == "succeeded"
    assert receipt.changed_count == 1
    assert receipt.targets == ()
    assert receipt.target_page is not None
    assert receipt.target_page.target_count == 1
    assert [name for name, _ in boundary.calls] == ["own_detail", "independent-page"]


def test_http_large_receipt_page_uses_current_login_and_bounded_selection():
    service, boundary, _ = own_service()
    boundary.selected = None
    terminal = SchoolFactOwnTerminal(
        LOCATOR, NOW - timedelta(days=1),
        SchoolFactApplyResult(UUID(int=91), 13, 4097, True), 4097,
    )
    boundary.read_own_target_page = lambda **kw: SchoolFactOwnTargetPage(
        terminal,
        (SchoolFactOwnResultTarget("official_contact", PRIVATE_SOURCE, "upsert", 3),),
        kw["offset"], 4097, False,
    )
    client, state, _ = client_for(service)
    state["session"] = "new-session"
    path = BASE + "/history-receipts/sf2receipt_" + LOCATOR
    response = client.get(path, params={"target_offset": 4096, "target_limit": 20})
    assert response.status_code == 200, response.text
    assert response.json()["target_page"]["target_count"] == 4097
    assert len(response.json()["targets"]) == 1
    assert PRIVATE_SOURCE not in response.text
    assert client.get(path, params={"target_limit": 21}).status_code == 422


def test_continuation_hint_reads_the_same_original_writer_targets_without_old_query_history():
    from hashlib import sha256
    from types import SimpleNamespace

    from app.composition.school_fact_directory_gateway import DirectoryFactToolGateway
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    service, boundary, _ = own_service()
    _, _, _, payload = setup()
    identity = SimpleNamespace(actor=actor(), session_binding="old-session")
    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: identity, query=SimpleNamespace(),
        product=service, retention_seconds=60,
    )
    context = ToolCallContext("actor", "conversation", "turn", "run", 1, uuid4())
    prepared = gateway.execute(ModelToolCall(
        call_id="prepare", name="school_facts.directory.prepare",
        arguments=payload.model_dump(mode="json"),
    ), context)
    reference = prepared.continuation.reference
    service.confirm(actor=actor(), session_binding="old-session",
                    proposal_ref=reference, request_id=uuid4())
    boundary.selected = own_result(sha256(reference.encode("ascii")).hexdigest())
    evidence = prepared.continuation.load()
    assert not any(name == "own_detail" for name, _ in boundary.calls)
    hint = evidence.model_content["tool_context"]["read_original_targets"]
    recovered = gateway.execute(ModelToolCall(
        call_id="original-targets", name=hint["tool"], arguments=hint["arguments"],
    ), context)
    assert recovered.fact_card["reference"] == hint["arguments"]["receipt_ref"]
    assert recovered.fact_card["targets"][0]["fact_kind"] == "official_contact"
    assert recovered.fact_card["targets"][0]["reference"]
    assert PRIVATE_SOURCE not in str(recovered.model_content)
    assert len([name for name, _ in boundary.calls if name == "confirm"]) == 1
    assert len([name for name, _ in boundary.calls if name == "own_detail"]) == 1


def listing(service, limit=5):
    return service.own_receipts(
        actor=actor(), session_binding="new-session", request_id=uuid4(), limit=limit
    )


def test_own_directory_is_bounded_and_references_are_signed_for_current_query():
    service, boundary, adapter = own_service()
    page = listing(service, 1)
    assert page.kind == "own_fact_receipts" and page.has_more is True
    assert len(page.items) == 1 and "不代表全部" in page.notice
    receipt = page.items[0]
    assert receipt.kind == "own_fact_receipt"
    assert "不证明当前目标仍存在或其字段可读" in receipt.message
    assert "当前可见性和时间有效性" in page.notice
    assert receipt.reference == "sf2receipt_" + LOCATOR
    assert receipt.status == "succeeded" and receipt.read_only is True
    assert receipt.confirmed_at == NOW - timedelta(days=1)
    assert receipt.changed_count == 1 and receipt.target_version == 13
    target = receipt.targets[0]
    assert target.fact_kind == "official_contact" and target.label == FACT_LABELS[target.fact_kind]
    assert target.operation == "upsert" and target.target_version == 3
    assert (
        adapter.repository.source_for(
            actor=actor(), fact_kind=target.fact_kind, reference=target.reference
        )
        == PRIVATE_SOURCE
    )
    text = page.model_dump_json()
    for forbidden in (PRIVATE_SOURCE, "old-session", "source_key", "credential", "before"):
        assert forbidden not in text
    name, kwargs = boundary.calls[0]
    assert name == "own_list" and kwargs["session_binding"] == "new-session"
    assert kwargs["limit"] == 1 and kwargs["context"].actor == actor()
    assert set(kwargs) == {"context", "session_binding", "limit"}
    assert len(boundary.calls) == 1


def test_exact_selection_never_falls_back_to_latest_or_rehashes_locator():
    service, boundary, _ = own_service()
    boundary.selected = own_result(OTHER)
    assert detail(service, "sf2receipt_" + OTHER).reference == "sf2receipt_" + OTHER
    assert [name for name, _ in boundary.calls] == ["own_detail"]
    assert boundary.calls[0][1]["locator_sha256"] == OTHER


def test_read_permission_does_not_require_old_maintenance_permission():
    service, boundary, _ = own_service()

    def denied(_actor):
        raise PermissionError("old maintenance denied")

    service._require_receipt = denied
    assert detail(service).status == "succeeded" and len(boundary.calls) == 1


@pytest.mark.parametrize("limit", [0, 21, True, 1.5, "2"])
def test_invalid_limit_never_reaches_boundary(limit):
    service, boundary, _ = own_service()
    with pytest.raises(ValueError):
        listing(service, limit)
    assert boundary.calls == []


@pytest.mark.parametrize(
    "reference",
    ["sf3plan_" + LOCATOR, "sf2receipt_" + "x" * 64, "sf2receipt_", "sf2receipt_" + LOCATOR + "a"],
)
def test_invalid_recovery_reference_never_reaches_boundary(reference):
    service, boundary, _ = own_service()
    with pytest.raises(ValueError):
        detail(service, reference)
    assert boundary.calls == []


def test_missing_and_denied_detail_have_same_safe_result():
    service, boundary, _ = own_service()
    boundary.selected = None
    with pytest.raises(ContactProductUnavailable) as missing:
        detail(service)
    boundary.error = SchoolFactChangeRejected("private scope explanation")
    with pytest.raises(ContactProductUnavailable) as denied:
        detail(service)
    assert str(missing.value) == str(denied.value)
    assert "private" not in str(denied.value)


def test_other_locator_cannot_be_published_as_selected_receipt():
    service, boundary, _ = own_service()
    boundary.selected = own_result(OTHER)
    with pytest.raises(ContactProductUnavailable):
        detail(service)
    assert [name for name, _ in boundary.calls] == ["own_detail"]


def test_source_failure_is_not_successful_empty_history():
    service, boundary, _ = own_service()
    boundary.error = RuntimeError("private source failure")
    with pytest.raises(RuntimeError):
        listing(service)


def test_own_read_cannot_unlock_old_receipt_or_confirmation():
    service, boundary, _ = own_service()
    reference = "sf2receipt_" + LOCATOR
    assert detail(service).status == "succeeded"
    with pytest.raises(ContactProductUnavailable):
        service.confirm(
            actor=actor(), session_binding="new-session", request_id=uuid4(), proposal_ref=reference
        )
    with pytest.raises(ContactProductUnavailable):
        service.receipt(
            actor=actor(),
            session_binding="new-session",
            request_id=uuid4(),
            proposal_ref="sf3plan_original",
        )
    assert not any(name == "confirm" for name, _ in boundary.calls)
    assert boundary.calls[-1][1]["session_binding"] == "new-session"


def test_http_list_and_detail_use_current_identity_and_no_store():
    service, boundary, _ = own_service()
    client, state, _ = client_for(service)
    state["session"] = "new-session"
    result = client.get(BASE + "/history-receipts", params={"limit": 1})
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["has_more"] is True and len(result.json()["items"]) == 1
    ref = result.json()["items"][0]["reference"]
    response = client.get(BASE + "/history-receipts/" + ref)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store" and response.json()["read_only"] is True
    assert boundary.calls[-1][1]["session_binding"] == "new-session"
    assert PRIVATE_SOURCE not in result.text + response.text


@pytest.mark.parametrize("suffix", ["?actor=other", "?limit=21", "?limit=0", "?limit=invalid"])
def test_http_list_rejects_extra_authority_and_invalid_limit(suffix):
    service, boundary, _ = own_service()
    client, _, _ = client_for(service)
    assert client.get(BASE + "/history-receipts" + suffix).status_code == 422
    assert boundary.calls == []


@pytest.mark.parametrize(
    "path,kwargs",
    [
        ("/history-receipts", {"json": {"actor": "other"}}),
        ("/history-receipts/sf2receipt_" + LOCATOR, {"params": {"actor": "other"}}),
        ("/history-receipts/sf2receipt_" + LOCATOR, {"json": {"confirm": True}}),
    ],
)
def test_http_history_rejects_body_and_detail_extra_parameters(path, kwargs):
    service, boundary, _ = own_service()
    client, _, _ = client_for(service)
    assert client.request("GET", BASE + path, **kwargs).status_code == 422
    assert boundary.calls == []


def test_http_missing_and_denied_match_and_source_failure_is_503():
    service, boundary, _ = own_service()
    client, _, _ = client_for(service)
    path = BASE + "/history-receipts/sf2receipt_" + LOCATOR
    boundary.selected = None
    missing = client.get(path)
    boundary.error = SchoolFactChangeRejected("private scope denial")
    denied = client.get(path)
    assert missing.status_code == denied.status_code == 409
    assert missing.json()["code"] == denied.json()["code"]
    assert missing.json()["message"] == denied.json()["message"]
    boundary.error = RuntimeError("private source failure")
    failed = client.get(BASE + "/history-receipts")
    assert failed.status_code == 503 and "private" not in failed.text


def f3_receipt_service(boundary):
    existing, _, _, _ = setup()
    return DirectoryFactProductService(
        boundary=cast(SchoolFactChangeBoundary, boundary),
        resolve_change=existing._resolve,
        require_maintenance=existing._require,
        require_receipt=lambda _actor: None,
        plan_store=InMemorySchoolFactPlanStore(clock=lambda: NOW),
        receipt_reference_for=lambda *, actor, fact_kind, source_key: (
            f"f3:{fact_kind}:opaque"
        ),
        receipt_fact_labels={
            "student": "学生基础资料",
            "guardian_relationship": "已核实家校关系",
        },
        receipt_reference_prefix="f3",
        receipt_uses_current_targets=True,
        clock=lambda: NOW,
    )


def test_f3_original_receipt_requires_current_target_aware_lookup():
    proposal = "sf3plan_" + "a" * 32
    locator = __import__("hashlib").sha256(proposal.encode("ascii")).hexdigest()
    boundary = OwnBoundary()
    # Simulates B being removed from the current allowed-target set while A remains.
    boundary.selected = None
    service = f3_receipt_service(boundary)
    with pytest.raises(ContactProductUnavailable):
        service.receipt(
            actor=actor(),
            session_binding="original-session",
            proposal_ref=proposal,
            request_id=uuid4(),
        )
    assert [name for name, _ in boundary.calls] == ["own_detail"]
    assert boundary.calls[0][1]["locator_sha256"] == locator
    assert boundary.calls[0][1]["session_binding"] == "original-session"
    assert "allowed_targets" not in boundary.calls[0][1]


@pytest.mark.parametrize("kind", ["student", "guardian_relationship"])
def test_f3_current_target_receipt_and_own_projection_use_f3_reference(kind):
    proposal = "sf3plan_" + "b" * 32
    locator = __import__("hashlib").sha256(proposal.encode("ascii")).hexdigest()
    result = SchoolFactOwnResult(
        locator_sha256=locator,
        confirmed_at=NOW,
        result=SchoolFactApplyResult(UUID(int=92), 14, 1, False),
        targets=(
            SchoolFactOwnResultTarget(
                kind, f"{kind}:b", "end" if kind == "guardian_relationship" else "upsert", 4
            ),
        ),
    )
    boundary = OwnBoundary()
    boundary.selected = result
    boundary.items = (result, result)
    service = f3_receipt_service(boundary)
    receipt = service.receipt(
        actor=actor(),
        session_binding="original-session",
        proposal_ref=proposal,
        request_id=uuid4(),
    )
    assert receipt.status == "succeeded" and receipt.target_version == 14
    recovered = service.own_receipt(
        actor=actor(),
        session_binding="new-login-session",
        request_id=uuid4(),
        receipt_ref="sf2receipt_" + locator,
    )
    assert recovered.targets[0].fact_kind == kind
    assert recovered.targets[0].reference == f"f3:{kind}:opaque"
    assert recovered.targets[0].operation == (
        "end" if kind == "guardian_relationship" else "upsert"
    )


def test_f3_target_aware_receipt_does_not_fall_back_to_old_locator_result():
    proposal = "sf3plan_" + "c" * 32
    boundary = OwnBoundary()
    boundary.selected = None
    boundary.result = SchoolFactApplyResult(UUID(int=93), 99, 1, False)
    service = f3_receipt_service(boundary)
    with pytest.raises(ContactProductUnavailable):
        service.receipt(
            actor=actor(),
            session_binding="original-session",
            proposal_ref=proposal,
            request_id=uuid4(),
        )
    assert not any(name == "old_receipt" for name, _ in boundary.calls)


def test_shared_own_history_keeps_full_page_and_each_original_target():
    boundary = OwnBoundary()
    family = replace(
        own_result(), targets=(SchoolFactOwnResultTarget("student", "family-student", "upsert", 3),)
    )
    boundary.items = tuple(
        replace(family if index < 10 else own_result(), locator_sha256=f"{index:064x}")
        for index in range(21)
    )
    service = f3_receipt_service(boundary)

    def project(current_actor, target):
        assert current_actor == actor()
        prefix = "f3" if target.fact_kind == "student" else "f2"
        return OwnReceiptTarget(
            fact_kind=target.fact_kind,
            label="学生基础资料" if prefix == "f3" else "官方联系方式",
            reference=f"{prefix}:{target.fact_kind}:opaque",
            operation=target.operation,
            target_version=target.target_version,
        )

    service._receipt_target_projection = project
    page = listing(service, 20)
    assert len(page.items) == 20 and page.has_more is True
    assert [item.reference for item in page.items] == [
        "sf2receipt_" + item.locator_sha256 for item in boundary.items[:20]
    ]
    assert page.items[0].targets[0].reference == "f3:student:opaque"
    assert page.items[10].targets[0].reference == "f2:official_contact:opaque"
    assert all(item.changed_count == len(item.targets) == 1 for item in page.items)
    boundary.selected = boundary.items[10]
    assert detail(service, page.items[10].reference).targets == page.items[10].targets
    assert [name for name, _ in boundary.calls] == ["own_list", "own_detail"]


def test_shared_receipt_projection_never_runs_after_current_authorization_denial():
    service, boundary, _ = own_service()
    boundary.error = SchoolFactChangeRejected("current scope revoked")
    calls = []
    service._receipt_target_projection = lambda *args: calls.append(args)
    with pytest.raises(ContactProductUnavailable):
        listing(service, 20)
    assert not calls


def shared_projection():
    return shared_own_receipt_projection(
        configuration=FamilyConfiguration(
            approval_reference="approved-f3",
            scope_bindings=(),
            source_catalog={"person": ("family-person",)},
            creation_catalog={"assignment": {"reserved-assignment": "预留任职"}},
        ),
        directory_reference_for=lambda **kw: f"f2:{kw['fact_kind']}:opaque",
        family_reference_for=lambda **kw: f"f3:{kw['fact_kind']}:opaque",
    )


@pytest.mark.parametrize(
    ("kind", "source", "prefix"),
    [
        ("person", "family-person", "f3"),
        ("person", "family-person-near", "f2"),
        ("assignment", "reserved-assignment", "f3"),
        ("assignment", "other-assignment", "f2"),
        ("student", "current-student", "f3"),
        ("official_contact", "directory-contact", "f2"),
    ],
)
def test_shared_receipt_projection_routes_exact_coordinate_not_shared_kind(kind, source, prefix):
    result = shared_projection()(actor(), SchoolFactOwnResultTarget(kind, source, "end", 8))
    assert result.reference == f"{prefix}:{kind}:opaque"
    assert (result.fact_kind, result.operation, result.target_version) == (kind, "end", 8)


def test_shared_receipt_keeps_mixed_batch_complete_and_rejects_unprojectable_target():
    service, boundary, _ = own_service()
    service._receipt_target_projection = shared_projection()
    boundary.selected = replace(
        own_result(),
        result=SchoolFactApplyResult(UUID(int=91), 13, 2, False),
        targets=(
            SchoolFactOwnResultTarget("person", "family-person", "upsert", 3),
            SchoolFactOwnResultTarget("person", "directory-person", "end", 4),
        ),
    )
    result = detail(service)
    assert result.changed_count == len(result.targets) == 2
    assert [target.reference for target in result.targets] == [
        "f3:person:opaque", "f2:person:opaque"
    ]
    boundary.selected = replace(
        boundary.selected,
        targets=(*boundary.selected.targets, SchoolFactOwnResultTarget("unknown", "x", "end", 4)),
    )
    with pytest.raises(ContactProductUnavailable):
        detail(service)


def test_both_http_history_pages_accept_full_shared_receipts_without_filtering():
    from types import SimpleNamespace

    from tests.test_f3_family_staff_http import BASE as FAMILY_BASE
    from tests.test_f3_family_staff_http import client as family_client

    boundary = OwnBoundary()
    family = replace(
        own_result(), targets=(SchoolFactOwnResultTarget("person", "family-person", "upsert", 3),)
    )
    boundary.items = tuple(
        replace(family if index < 10 else own_result(), locator_sha256=f"{index:064x}")
        for index in range(21)
    )
    service = f3_receipt_service(boundary)
    service._receipt_target_projection = shared_projection()
    f2_client, _, _ = client_for(service)
    f3_client = family_client(
        SimpleNamespace(actor=actor(), session_binding="current-session"), product=service
    )
    for http, base in ((f2_client, BASE), (f3_client, FAMILY_BASE)):
        response = http.get(base + "/history-receipts?limit=20")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        data = response.json()
        assert data["kind"] == "own_fact_receipts" and data["has_more"] is True
        assert len(data["items"]) == 20
        assert data["items"][0]["targets"][0]["reference"] == "f3:person:opaque"
        assert data["items"][10]["targets"][0]["reference"] == "f2:official_contact:opaque"
        assert "source_key" not in response.text and PRIVATE_SOURCE not in response.text



def test_family_http_large_receipt_detail_accepts_bounded_target_page():
    from types import SimpleNamespace

    from tests.test_f3_family_staff_http import BASE as FAMILY_BASE
    from tests.test_f3_family_staff_http import client as family_client

    boundary = OwnBoundary()
    boundary.selected = None
    terminal = SchoolFactOwnTerminal(
        LOCATOR, NOW - timedelta(days=1),
        SchoolFactApplyResult(UUID(int=91), 13, 4097, True), 4097,
    )
    boundary.read_own_target_page = lambda **kw: SchoolFactOwnTargetPage(
        terminal,
        (SchoolFactOwnResultTarget("person", "family-person", "upsert", 3),),
        kw["offset"], 4097, False,
    )
    service = f3_receipt_service(boundary)
    service._receipt_target_projection = shared_projection()
    http = family_client(
        SimpleNamespace(actor=actor(), session_binding="current-session"), product=service
    )
    path = FAMILY_BASE + "/history-receipts/sf2receipt_" + LOCATOR
    response = http.get(path, params={"target_offset": 4096, "target_limit": 20})
    assert response.status_code == 200, response.text
    assert response.json()["target_page"]["target_count"] == 4097
    assert len(response.json()["targets"]) == 1
    assert http.get(path, params={"target_limit": 21}).status_code == 422


def test_original_fields_request_is_finite_and_keeps_the_original_receipt_metadata():
    service, boundary, _ = own_service()
    expected = detail(service)
    received, raw = service.own_receipt_original(
        actor=actor(), session_binding="new-session", request_id=uuid4(),
        receipt_ref="sf2receipt_" + LOCATOR, target_offset=1, target_limit=2)
    assert received.model_dump(exclude={"request_id"}) == expected.model_dump(
        exclude={"request_id"}
    )
    assert raw is boundary.selected
    _, args = boundary.calls[-1]
    assert args["original_fields"] == {"teaching_assignment": (
        "assignment_source_key", "offering_source_key", "teaching_role")}
    assert args["target_offset"] == 1 and args["target_limit"] == 2
    assert args["locator_sha256"] == LOCATOR


@pytest.mark.parametrize("bad", [{"target_offset": -1}, {"target_limit": 4},
    {"target_limit": True}, {"receipt_ref": "sf2receipt_" + "Z" * 64}])
def test_original_fields_invalid_selection_does_not_reach_writer(bad):
    service, boundary, _ = own_service()
    with pytest.raises(ValueError):
        service.own_receipt_original(actor=actor(), session_binding="new-session",
            request_id=uuid4(), **{"receipt_ref": "sf2receipt_" + LOCATOR, **bad})
    assert boundary.calls == []


@pytest.mark.parametrize("changed", ["original_fields", "metadata", "identity", "name_permission"])
def test_original_operation_projection_keeps_all_release_dependencies(monkeypatch, changed):
    from copy import deepcopy
    from types import SimpleNamespace

    from app.composition import school_fact_receipt_teaching as names
    from app.composition.school_fact_directory_gateway import OwnReceiptInput
    from app.composition.school_fact_product import read_receipt_original_teaching
    from app.platform.assistant.tool_loop import ToolCallContext

    original_fields = SimpleNamespace(
        status="available",
        field_names=("assignment_source_key",),
        segments=("exact-revision-segment",),
        before_segments=(),
        field_changes=True,
    )
    original = SimpleNamespace(
        targets=(
            SimpleNamespace(
                fact_kind="teaching_assignment",
                source_key="exact-key",
                original_result=original_fields,
            ),
        )
    )
    original_view = {"reference": "sf2receipt_" + LOCATOR,
        "targets": [{"reference": "f4:teaching_assignment:" + "f" * 64}]}
    state = {"identity": SimpleNamespace(actor=actor(), session_binding="new-session"),
             "view": deepcopy(original_view), "names_allowed": True}
    requests = []
    def get(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(model_dump=lambda **_: deepcopy(state["view"])), deepcopy(original)
    def verify_name(**kwargs):
        if not state["names_allowed"]:
            raise PermissionError("name scope changed")
    def project(selected_actor, segments, **kwargs):
        assert selected_actor == actor() and segments == ("exact-revision-segment",)
        return SimpleNamespace(material={"items": [{"teacher_name": "Original teacher"}]},
            proof=SimpleNamespace(revalidate=verify_name))
    monkeypatch.setattr(names, "project_original_teaching", project)
    teaching = SimpleNamespace(
        identity=lambda _: state["identity"],
        query=SimpleNamespace(
            external_reader=object(),
            repository=SimpleNamespace(
                clock=lambda: NOW,
                authority=SimpleNamespace(
                    require_scope=lambda *a, **k: object(),
                    require_object=lambda *a, **k: None,
                    require_fields=lambda *a, **k: None,
                    revalidate_many=lambda *a, **k: None,
                ),
            ),
        ),
    )
    directory = SimpleNamespace(product=SimpleNamespace(own_receipt_original=get))
    selected = OwnReceiptInput(receipt_ref=original_view["reference"], include_original=True)
    result = read_receipt_original_teaching(directory, teaching, original_view, selected,
        ToolCallContext("identity", "conversation", "turn", "run", 1, uuid4()))
    assert (
        result.model_content["original_results"][0]["result"]["items"][0]["teacher_name"]
        == "Original teacher"
    )
    assert all(r["receipt_ref"] == original_view["reference"] for r in requests)
    assert result.history_check.valid()
    if changed == "original_fields":
        original.targets[0].original_result.status = "denied"
    elif changed == "metadata":
        state["view"]["reference"] = "different-original"
    elif changed == "identity":
        state["identity"] = SimpleNamespace(actor=actor(), session_binding="other-login")
    else:
        state["names_allowed"] = False
    assert not result.history_check.valid()
    assert not result.history_check.refresh()


@pytest.mark.parametrize("has_after_segments", [False, True])
def test_original_field_permission_revoked_by_later_io_blocks_batch_release(
    monkeypatch, has_after_segments
):
    from copy import deepcopy
    from types import SimpleNamespace

    from app.composition import school_fact_receipt_teaching as names
    from app.composition.school_fact_directory_gateway import OwnReceiptInput
    from app.composition.school_fact_education_authority import EducationAuthority
    from app.composition.school_fact_education_settings import EducationConfiguration
    from app.composition.school_fact_product import read_receipt_original_teaching
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.assistant.tool_loop import ToolCallContext
    from app.platform.authorization.thin_service import ThinAuthorizationService
    from app.platform.governance.contracts import GovernanceDependencyVersion
    from app.platform.school_facts.f5_policy import F5FactPolicy, F5FactScope
    from tests.governance.test_f4_authority import NOW as READ_AT
    from tests.governance.test_f4_authority import Governance
    from tests.governance.test_f4_authority import actor as read_actor

    selected_actor = read_actor()
    field_names = ("assignment_source_key", "offering_source_key", "teaching_role")
    source_key = "original-assignment"
    scope = F5FactScope(
        selected_actor.school_id, "teaching_assignment", (source_key,), (), field_names
    )

    class TeachingGovernance(Governance):
        def issue(self, query):
            basis = super().issue(query)
            return replace(basis, dependency_versions=tuple(
                GovernanceDependencyVersion(
                    dependency.authority.replace("school_facts.f4", "school_facts.f5"),
                    dependency.reference, dependency.version,
                )
                for dependency in basis.dependency_versions
            ))

    scope_id = uuid4()
    governance = TeachingGovernance(scope, scope_id)
    authority = EducationAuthority(
        thin=ThinAuthorizationService(
            governance_basis_service=governance, module_policies={"school_facts.f5": F5FactPolicy()}
        ),
        configuration=EducationConfiguration(
            approval_reference="original-field-release-test",
            scope_bindings=({
                "assignment_id": selected_actor.assignment_id,
                "scope_id": scope_id,
                "operation_code": "school_facts.f5.teaching_assignment.read",
            },),
        ),
        clock=lambda: READ_AT,
    )
    reference = "sf2receipt_" + LOCATOR
    view = {
        "reference": reference,
        "targets": [{"reference": "f4:teaching_assignment:" + "f" * 64}],
    }
    segment = SimpleNamespace(effective_at=READ_AT, valid_to=None)
    original = SimpleNamespace(targets=(SimpleNamespace(
        fact_kind="teaching_assignment", source_key=source_key,
        original_result=SimpleNamespace(
            status="available", field_names=field_names,
            segments=(segment,) if has_after_segments else (),
            before_segments=() if has_after_segments else (segment,), field_changes=True,
        ),
    ),))
    monkeypatch.setattr(names, "project_original_teaching", lambda *_a, **_k: SimpleNamespace(
        material={"items": [{"teacher_name": "合成原教师"}] if has_after_segments else []},
        proof=SimpleNamespace(revalidate=lambda **_k: None),
    ))
    directory = SimpleNamespace(product=SimpleNamespace(own_receipt_original=lambda **_k: (
        SimpleNamespace(model_dump=lambda **_k: deepcopy(view)), deepcopy(original)
    )))
    teaching = SimpleNamespace(
        identity=lambda _ref: SimpleNamespace(actor=selected_actor, session_binding="same-session"),
        query=SimpleNamespace(external_reader=object(), repository=SimpleNamespace(
            authority=authority, clock=lambda: READ_AT,
        )),
    )
    result = read_receipt_original_teaching(
        directory, teaching, view, OwnReceiptInput(receipt_ref=reference, include_original=True),
        ToolCallContext("identity", "conversation", "turn", "run", 1, uuid4()),
    )
    assert result.history_check.valid()

    with pytest.raises(AssistantRuntimeError, match="history authority changed during validation"):
        with validation_batch():
            assert result.history_check.valid()
            # A later name/current dependency performs IO after the original
            # check. Its metadata and original values stay unchanged.
            governance.valid = False
    assert not result.history_check.valid()


def test_original_version_reread_is_once_per_release_batch_and_fresh_next_batch(monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace

    from app.composition import school_fact_receipt_teaching as names
    from app.composition.school_fact_directory_gateway import OwnReceiptInput
    from app.composition.school_fact_product import read_receipt_original_teaching
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.assistant.tool_loop import ToolCallContext

    reference = "sf2receipt_" + LOCATOR
    view = {
        "reference": reference,
        "targets": [{"reference": "f4:teaching_assignment:" + "f" * 64}],
    }
    original = SimpleNamespace(targets=(SimpleNamespace(
        fact_kind="teaching_assignment", source_key="exact-original-key",
        original_result=SimpleNamespace(
            status="available", field_names=("assignment_source_key",),
            segments=("original-segment",), before_segments=(), field_changes=True,
        ),
    ),))
    reads = []

    def read(**kwargs):
        reads.append(kwargs)
        return SimpleNamespace(model_dump=lambda **_k: deepcopy(view)), deepcopy(original)

    monkeypatch.setattr(names, "project_original_teaching", lambda *_a, **_k: SimpleNamespace(
        material={"items": [{"teacher_name": "合成原教师"}]},
        proof=SimpleNamespace(revalidate=lambda **_k: None),
    ))
    directory = SimpleNamespace(product=SimpleNamespace(own_receipt_original=read))
    teaching = SimpleNamespace(
        identity=lambda _ref: SimpleNamespace(actor=actor(), session_binding="same-session"),
        query=SimpleNamespace(external_reader=object(), repository=SimpleNamespace(
            clock=lambda: NOW,
            authority=SimpleNamespace(
                require_scope=lambda *_a, **_k: object(),
                require_object=lambda *_a, **_k: None,
                require_fields=lambda *_a, **_k: None,
                revalidate_many=lambda *_a, **_k: None,
            ),
        )),
    )
    result = read_receipt_original_teaching(
        directory, teaching, view, OwnReceiptInput(receipt_ref=reference, include_original=True),
        ToolCallContext("identity", "conversation", "turn", "run", 1, uuid4()),
    )
    baseline = len(reads)
    for number in (1, 2):
        with validation_batch():
            for _ in range(3):
                assert result.history_check.valid()
            assert len(reads) == baseline + number - 1
        assert len(reads) == baseline + number
    assert all(request["receipt_ref"] == reference for request in reads)

    with pytest.raises(AssistantRuntimeError, match="Authorized full-read evidence changed"):
        with validation_batch():
            assert result.history_check.valid()
            original.targets[0].original_result.status = "denied"
    assert len(reads) == baseline + 3
