"""Large own receipts recover a terminal result without materializing all targets."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine

from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from app.platform.authorization.secure_reader_contracts import SecureReadDenied
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.ports import (
    SchoolFactApplyResult,
    SchoolFactOwnResultTarget,
    SchoolFactOwnTargetPage,
    SchoolFactOwnTerminal,
)
from tests import test_f2_own_receipts as original_receipts
from tests.school_facts.test_f2_own_result_executor import _OwnKernel
from tests.school_facts.test_sf5b_writer_executor import (
    CALLER_KEY,
    CONTEXT_KEY,
    NOW,
    _writer_settings,
)
from tests.school_facts.test_sf5b_writer_executor import _context as writer_context

context = original_receipts.context


def _reject_packages_in_own_receipt_fixture(*_args):
    raise SchoolFactChangeRejected("Package writes are disabled in the own-receipt fixture.")


def test_v55_own_receipt_kernel_still_requires_a_package_final_guard():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    settings = _writer_settings().model_copy(
        update={"schema_contract": "school-facts-f7-v55"}
    )
    try:
        with pytest.raises(ValueError, match="package final authorization guard"):
            SqlAlchemySchoolFactWriterExecutorKernel(
                engine=engine,
                settings=settings,
                final_authorization_guard=lambda *_: None,
                family_actor_validator=object(),
            )
        kernel = SqlAlchemySchoolFactWriterExecutorKernel(
            engine=engine,
            settings=settings,
            final_authorization_guard=lambda *_: None,
            family_actor_validator=object(),
            package_final_authorization_guard=_reject_packages_in_own_receipt_fixture,
        )
        assert kernel is not None
        guard = kernel._package_final_authorization_guard
        assert callable(guard)
        with pytest.raises(SchoolFactChangeRejected, match="Package writes are disabled"):
            guard(None, None, None)
    finally:
        engine.dispose()


@pytest.fixture
def receipt_store(monkeypatch):
    original = original_receipts.store.__wrapped__(monkeypatch)
    try:
        yield next(original)
    finally:
        next(original, None)


def test_original_actor_can_locate_a_4097_target_terminal_without_old_login(receipt_store):
    service, seed, _ = receipt_store
    targets = frozenset(("official_contact", f"contact:{n:05d}") for n in range(4097))
    locator = seed(1, targets=targets)

    result = service.check_own_terminal_by_locator(context=context(), locator_sha256=locator)

    assert result is not None
    assert result.locator_sha256 == locator
    assert result.result.changed_count == 4097
    assert result.target_count == 4097
    assert not hasattr(result, "targets")
    assert "previous-login" not in repr(result)


def test_terminal_locator_does_not_cross_owner_or_school(receipt_store):
    service, seed, _ = receipt_store
    locator = seed(2)
    assert (
        service.check_own_terminal_by_locator(context=context(user=8), locator_sha256=locator)
        is None
    )
    assert (
        service.check_own_terminal_by_locator(
            context=context(person=UUID(int=99)), locator_sha256=locator
        )
        is None
    )
    assert (
        service.check_own_terminal_by_locator(
            context=context(school=UUID(int=99)), locator_sha256=locator
        )
        is None
    )


def test_terminal_is_not_success_when_result_or_batch_is_incomplete(receipt_store):
    service, seed, _ = receipt_store
    pending = seed(3, status="pending")
    incomplete = seed(4, complete=False)
    missing_result = seed(5, has_result=False)
    for locator in (pending, incomplete, missing_result):
        assert (
            service.check_own_terminal_by_locator(context=context(), locator_sha256=locator)
            is None
        )


def test_large_receipt_coordinates_are_bounded_and_stable_across_pages(receipt_store):
    service, seed, _ = receipt_store
    targets = frozenset(("official_contact", f"contact:{n:05d}") for n in range(4097))
    locator = seed(6, targets=targets)

    first = service.read_own_target_page(
        context=context(), locator_sha256=locator, offset=0, limit=20
    )
    last = service.read_own_target_page(
        context=context(), locator_sha256=locator, offset=4090, limit=20
    )

    assert first is not None and last is not None
    assert first.terminal.target_count == last.terminal.target_count == 4097
    assert [item.source_key for item in first.items] == [
        f"contact:{n:05d}" for n in range(20)
    ]
    assert [item.source_key for item in last.items] == [
        f"contact:{n:05d}" for n in range(4090, 4097)
    ]
    assert first.has_more is True and last.has_more is False
    assert (first.offset, first.next_offset) == (0, 20)
    assert (last.offset, last.next_offset) == (4090, 4097)


def test_fifty_thousand_target_terminal_and_final_page_stay_bounded(receipt_store):
    service, seed, _ = receipt_store
    locator = seed(
        9,
        targets=frozenset(("official_contact", f"contact:{n:05d}") for n in range(50_000)),
    )
    page = service.read_own_target_page(
        context=context(), locator_sha256=locator, offset=49_980, limit=20
    )
    assert page is not None
    assert page.terminal.target_count == page.terminal.result.changed_count == 50_000
    assert len(page.items) == 20
    assert page.items[0].source_key == "contact:49980"
    assert page.items[-1].source_key == "contact:49999"
    assert page.next_offset == 50_000 and page.has_more is False


@pytest.mark.parametrize("offset,limit", [(-1, 2), (0, 0), (0, 101), (True, 1)])
def test_large_receipt_page_rejects_unbounded_or_invalid_selection(receipt_store, offset, limit):
    service, seed, _ = receipt_store
    locator = seed(7)
    with pytest.raises(SchoolFactChangeRejected):
        service.read_own_target_page(
            context=context(), locator_sha256=locator, offset=offset, limit=limit
        )


def test_page_past_terminal_count_is_rejected_without_inconsistent_transport(receipt_store):
    service, seed, _ = receipt_store
    locator = seed(10)
    with pytest.raises(SchoolFactChangeRejected):
        service.read_own_target_page(
            context=context(), locator_sha256=locator, offset=2, limit=20
        )


def test_large_receipt_page_never_crosses_owner(receipt_store):
    service, seed, _ = receipt_store
    locator = seed(8)
    assert service.read_own_target_page(
        context=context(user=8), locator_sha256=locator, offset=0, limit=20
    ) is None


def test_writer_checks_current_duty_before_terminal_and_target_scope_before_release(monkeypatch):
    calls = []
    state = {"duty": True, "allowed": {"contact:a"}, "creation": set(), "revoked": False}
    terminal = SchoolFactOwnTerminal(
        "a" * 64, original_receipts.NOW,
        SchoolFactApplyResult(UUID(int=20), 2, 4097, True), 4097,
    )
    page = SchoolFactOwnTargetPage(
        terminal,
        (
            SchoolFactOwnResultTarget("official_contact", "contact:a", "upsert", 2),
            SchoolFactOwnResultTarget("official_contact", "contact:b", "upsert", 2),
        ),
        0,
        2,
        True,
    )

    class Proof:
        @property
        def allowed_targets(self):
            return frozenset(("official_contact", key) for key in state["allowed"])

        @property
        def creation_only_targets(self):
            return frozenset(("official_contact", key) for key in state["creation"])

        def revalidate(self):
            calls.append("revalidate")
            if state["revoked"]:
                raise SecureReadDenied("revoked")

    class Authorizer:
        def __call__(self, _context):
            calls.append("duty")
            if not state["duty"]:
                raise PermissionError("no current receipt duty")
            return Proof()

        def for_targets(self, _context, targets):
            calls.append(("targets", targets))
            if any(key not in state["allowed"] for _, key in targets):
                raise PermissionError("current target denied")
            return Proof()

    class Service:
        def read_own_target_page(self, **_kwargs):
            calls.append("read")
            return page

    @contextmanager
    def connect():
        yield object()

    monkeypatch.setattr(
        "app.composition.school_fact_writer_executor.sessionmaker", lambda **_: None
    )
    kernel = SqlAlchemySchoolFactWriterExecutorKernel(
        engine=SimpleNamespace(connect=connect),
        settings=_writer_settings(),
        own_result_authorizer=Authorizer(),
        own_terminal_authorizer=Authorizer(),
    )
    monkeypatch.setattr(kernel, "_service", lambda **_: Service())
    no_terminal_duty = SqlAlchemySchoolFactWriterExecutorKernel(
        engine=SimpleNamespace(connect=connect),
        settings=_writer_settings(),
        own_result_authorizer=Authorizer(),
    )
    monkeypatch.setattr(no_terminal_duty, "_service", lambda **_: Service())
    with pytest.raises(SchoolFactChangeRejected):
        no_terminal_duty.read_own_target_page(
            context=writer_context(), session_binding="new-login", locator_sha256="a" * 64,
            offset=0, limit=20,
        )
    assert calls == [], "ordinary fact read authorization cannot become terminal authority"
    state["duty"] = False
    with pytest.raises(PermissionError):
        kernel.read_own_target_page(
            context=writer_context(), session_binding="new-login", locator_sha256="a" * 64,
            offset=0, limit=20,
        )
    assert "read" not in calls

    state["duty"] = True
    result = kernel.read_own_target_page(
        context=writer_context(), session_binding="new-login", locator_sha256="a" * 64,
        offset=0, limit=20,
    )
    assert result is not None
    assert [target.source_key for target in result.items] == ["contact:a"]
    assert calls.index("duty") < calls.index("read")
    assert calls[-1] == "revalidate"

    state["allowed"] = {"contact:a", "contact:b"}
    state["creation"] = {"contact:b"}
    creation_only = kernel.read_own_target_page(
        context=writer_context(), session_binding="new-login", locator_sha256="a" * 64,
        offset=0, limit=20,
    )
    assert creation_only is not None
    assert [target.source_key for target in creation_only.items] == ["contact:a"]

    state["allowed"] = set()
    terminal_only = kernel.read_own_target_page(
        context=writer_context(), session_binding="new-login", locator_sha256="a" * 64,
        offset=0, limit=20,
    )
    assert terminal_only is not None and terminal_only.terminal.target_count == 4097
    assert terminal_only.items == ()
    assert terminal_only.next_offset == 2

    state["revoked"] = True
    with pytest.raises(SecureReadDenied):
        kernel.read_own_target_page(
            context=writer_context(), session_binding="new-login", locator_sha256="a" * 64,
            offset=0, limit=20,
        )


def test_writer_http_and_client_use_signed_context_and_bounded_page_without_old_session():
    from app.composition.school_fact_executor_client_settings import (
        SchoolFactExecutorClientSettings,
    )
    from app.composition.school_fact_writer_client import SchoolFactWriterExecutorClient
    from app.composition.school_fact_writer_executor import create_school_fact_writer_executor_app
    from app.platform.identity.executor_context_codec import CommandContextTransportCodec

    class PagedKernel(_OwnKernel):
        def read_own_target_page(self, *, context, session_binding, locator_sha256, offset, limit):
            assert session_binding == "new-login"
            assert locator_sha256 == "d" * 64
            assert (offset, limit) == (4096, 20)
            self.contexts.append(context)
            self.operations.append("target-page")
            return SchoolFactOwnTargetPage(
                SchoolFactOwnTerminal(
                    locator_sha256, NOW,
                    SchoolFactApplyResult(UUID(int=200), 7, 4097, True), 4097,
                ),
                (SchoolFactOwnResultTarget("school_service", "service-last", "upsert", 7),),
                4096,
                4097,
                False,
            )

    kernel = PagedKernel()
    codec = CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW)
    api = TestClient(create_school_fact_writer_executor_app(
        kernel=kernel, context_codec=codec, caller_auth_key=CALLER_KEY,
        enable_own_results=True,
    ))
    path = "/internal/v1/school-facts/own-results/target-page"
    body = {
        "context_token": codec.encode(writer_context()), "session_binding": "new-login",
        "locator_sha256": "d" * 64, "offset": 4096, "limit": 20,
    }
    assert api.post(path, json=body).status_code == 401
    assert api.post(path, headers={"X-School-Fact-Executor-Key": CALLER_KEY.decode()},
                    json={**body, "actor_user_id": 999}).status_code == 422
    assert kernel.operations == []

    def transport(request):
        payload = json.loads(request.content)
        assert set(payload) == set(body)
        assert codec.decode(payload["context_token"]) == writer_context()
        result = api.post(request.url.path, headers=dict(request.headers), json=payload)
        return httpx.Response(result.status_code, json=result.json())

    settings = SchoolFactExecutorClientSettings(
        base_url="http://writer:8010",
        caller_auth_key=SecretStr(CALLER_KEY.decode()),
        context_transport_hmac_key=SecretStr(CONTEXT_KEY.decode()),
    )
    with SchoolFactWriterExecutorClient(
        settings=settings, context_codec=codec, transport=httpx.MockTransport(transport)
    ) as client:
        result = client.read_own_target_page(
            context=writer_context(), session_binding="new-login", locator_sha256="d" * 64,
            offset=4096, limit=20,
        )
    assert result is not None
    assert result.terminal.target_count == 4097
    assert [(item.fact_kind, item.source_key) for item in result.items] == [
        ("school_service", "service-last")
    ]
    assert (result.offset, result.next_offset, result.has_more) == (4096, 4097, False)
    assert kernel.operations == ["target-page"]


@pytest.mark.parametrize("case", ["terminal_only", "revoked", "other_owner", "source_failure"])
def test_product_history_recovery_crosses_http_denial_without_bypassing_terminal_checks(case):
    from app.composition.school_fact_product_contacts import ContactProductUnavailable
    from app.composition.school_fact_writer_client import SchoolFactWriterExecutorUnavailable
    from app.composition.school_fact_writer_executor import create_school_fact_writer_executor_app
    from app.platform.identity.executor_context_codec import CommandContextTransportCodec
    from tests.school_facts.test_sf5b_writer_executor import _mock_executor_client
    from tests.test_f2_own_receipt_product import LOCATOR, actor, own_service

    seen = []

    class CurrentKernel(_OwnKernel):
        def check_own_result_by_locator(self, **kwargs):
            seen.append("current-target-check")
            assert kwargs["session_binding"] == "new-login"
            if case == "source_failure":
                raise RuntimeError("synthetic-internal-database-failure")
            raise PermissionError("current target details not readable")

        def read_own_target_page(self, *, context, session_binding, locator_sha256, offset, limit):
            seen.append("current-terminal-check")
            assert session_binding == "new-login" and locator_sha256 == LOCATOR
            if case == "revoked":
                raise SecureReadDenied("current terminal duty revoked")
            if case == "other_owner":
                return None
            assert (offset, limit) == (0, 20)
            return SchoolFactOwnTargetPage(
                SchoolFactOwnTerminal(LOCATOR, NOW,
                                     SchoolFactApplyResult(UUID(int=200), 7, 1, True), 1),
                (), 0, 1, False,
            )

    codec = CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW)
    api = TestClient(create_school_fact_writer_executor_app(
        kernel=CurrentKernel(), context_codec=codec, caller_auth_key=CALLER_KEY,
        enable_own_results=True,
    ), raise_server_exceptions=False)

    def transport(request):
        response = api.post(request.url.path, headers=dict(request.headers),
                            json=json.loads(request.content))
        return httpx.Response(response.status_code, content=response.content)

    service, _, _ = own_service()
    with _mock_executor_client(httpx.MockTransport(transport)) as client:
        service._boundary = client
        options = {"actor": actor(), "session_binding": "new-login", "request_id": UUID(int=22),
                   "receipt_ref": "sf2receipt_" + LOCATOR}
        if case == "terminal_only":
            result = service.own_receipt(**options)
            assert result.status == "succeeded" and result.changed_count == 1
            assert result.targets == () and result.read_only
            assert result.target_page.target_count == 1
        else:
            expected = (SchoolFactWriterExecutorUnavailable if case == "source_failure"
                        else ContactProductUnavailable)
            with pytest.raises(expected):
                service.own_receipt(**options)
    assert seen == (["current-target-check"] if case == "source_failure"
                    else ["current-target-check", "current-terminal-check"])


def test_model_material_continues_large_receipt_without_repaging_filtered_targets():
    from app.composition.school_fact_directory_action_material import own_receipt_model_material

    view = {
        "reference": "sf2receipt_" + "a" * 64,
        "kind": "own_fact_receipt",
        "status": "succeeded",
        "message": "原操作已确认成功",
        "changed_count": 4097,
        "target_version": 7,
        "request_id": str(UUID(int=8)),
        "confirmed_at": original_receipts.NOW.isoformat(),
        "read_only": True,
        "targets": [{
            "fact_kind": "official_contact", "label": "官方联系",
            "reference": "f2:official_contact:" + "b" * 64,
            "operation": "upsert", "target_version": 7,
        }],
        "target_page": {
            "offset": 4080, "next_offset": 4100, "target_count": 4097,
            "has_more": False,
        },
    }
    # An authorized page can omit revoked targets. Its continuation is based
    # on scanned lineage, not the number of visible targets.
    view["target_page"]["next_offset"] = 4097
    result = own_receipt_model_material(view, target_offset=4080, target_limit=20)
    assert result["tool_context"]["target_page"] == {
        "offset": 4080, "end": 4097, "total_count": 4097, "has_more": False
    }
    assert len(result["tool_context"]["targets"]) == 1
