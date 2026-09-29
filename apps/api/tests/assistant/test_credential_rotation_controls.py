"""Offline rotation checks using synthetic keys and the real control service."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count
from uuid import UUID

import pytest

from app.platform.assistant.catalog import AssistantModuleCatalogEntry, StaticAssistantCatalog
from app.platform.assistant.contracts import (
    ActionOfferV1,
    ModuleResultV1,
    RecoveryControlV1,
    ResultCheckHandleV1,
)
from app.platform.assistant.controls import (
    AssistantControlService,
    ControlIssueContext,
    ControlKind,
    ControlRejectedError,
)
from tests.assistant.test_control_service import (
    _ControlProbeModule,
    _direct_result,
    _MemoryControlStore,
    _runtime_hooks,
)

_KINDS: tuple[ControlKind, ...] = (
    "action_offer",
    "recovery_control",
    "result_check_handle",
)
_NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
_OLD_SYNTHETIC_KEY = bytes(range(32))
_NEW_SYNTHETIC_KEY = bytes(range(32, 64))


class _ObservedStore(_MemoryControlStore):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def get(self, *, credential_ref: str) -> str | None:
        self.reads += 1
        return super().get(credential_ref=credential_ref)


def _context() -> ControlIssueContext:
    return ControlIssueContext(
        actor_context_ref="actor-private-1",
        conversation_ref="conversation-1",
        turn_ref="turn-1",
        run_ref="run-1",
        generation=1,
        module_ref="fixture.control",
        module_state_ref="state-private-1",
        state_revision=1,
        issuance_request_id=UUID(int=101),
    )


def _result(kind: ControlKind) -> ModuleResultV1:
    return _direct_result(
        state_revision=1,
        staged_state_ref="state-private-1",
        action_offer=(
            ActionOfferV1(action_offer_ref="offer-private-1", safe_label="确认办理")
            if kind == "action_offer"
            else None
        ),
        recovery_control=(
            RecoveryControlV1(recovery_control_ref="recovery-private-1", safe_label="放弃内容")
            if kind == "recovery_control"
            else None
        ),
        result_check_handle=(
            ResultCheckHandleV1(result_check_ref="result-private-1")
            if kind == "result_check_handle"
            else None
        ),
    )


def _service(
    store: _ObservedStore,
    module: _ControlProbeModule,
    key: bytes,
    *,
    first_nonce: int = 1,
) -> AssistantControlService:
    nonces = count(first_nonce)
    return AssistantControlService(
        catalog=StaticAssistantCatalog(
            (
                AssistantModuleCatalogEntry(
                    module_ref="fixture.control",
                    model_safe_capability="固定控制探针。",
                    module=module,
                ),
            )
        ),
        store=store,
        signing_keys={"f1": key},
        active_key_id="f1",
        clock=lambda: _NOW,
        nonce_factory=lambda: f"{next(nonces):064x}",
        confirmation_ref_factory=lambda: "confirmation-private-1",
    )


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("resign_record", (False, True), ids=("old-record", "new-signed-record"))
def test_same_key_id_rotation_rejects_old_handle_before_store_or_module(
    kind: ControlKind, resign_record: bool
) -> None:
    store, module, context = _ObservedStore(), _ControlProbeModule(), _context()
    old = _service(store, module, _OLD_SYNTHETIC_KEY)
    old_control = old.issue_from_result(context=context, result=_result(kind))[0]
    rotated = _service(store, module, _NEW_SYNTHETIC_KEY, first_nonce=100)

    if resign_record:
        # Reissue through the public API with identical bindings, nonce and key id.
        # This makes a valid new-key record without duplicating the signing algorithm.
        rebuilt_store = _ObservedStore()
        rebuilt = _service(rebuilt_store, module, _NEW_SYNTHETIC_KEY)
        rebuilt_control = rebuilt.issue_from_result(context=context, result=_result(kind))[0]
        assert rebuilt_control.handle != old_control.handle
        assert rebuilt_store.records.keys() == store.records.keys()
        assert rebuilt_store.records != store.records
        store.records.update(rebuilt_store.records)

    before = dict(store.records)
    reservations: list[ControlIssueContext] = []

    def reserve(binding: ControlIssueContext) -> bool:
        reservations.append(binding)
        return binding == context

    with pytest.raises(ControlRejectedError):
        rotated.execute(
            handle=old_control.handle,
            expected_kind=kind,
            actor_context_ref=context.actor_context_ref,
            conversation_ref=context.conversation_ref,
            click_request_id=UUID(int=900),
            runtime_hooks=_runtime_hooks(reserve),
        )

    assert store.reads == 0
    assert store.records == before
    assert reservations == []
    assert module.confirm_calls == module.recover_calls == module.check_calls == []


@pytest.mark.parametrize("kind", _KINDS)
def test_rotated_key_issues_new_handle_that_calls_only_its_formal_entry(kind: ControlKind) -> None:
    store, module, context = _ObservedStore(), _ControlProbeModule(), _context()
    old = _service(store, module, _OLD_SYNTHETIC_KEY)
    old.issue_from_result(context=context, result=_result(kind))
    old_records = dict(store.records)
    rotated = _service(store, module, _NEW_SYNTHETIC_KEY, first_nonce=100)
    new_control = rotated.issue_from_result(context=context, result=_result(kind))[0]

    execution = rotated.execute(
        handle=new_control.handle,
        expected_kind=kind,
        actor_context_ref=context.actor_context_ref,
        conversation_ref=context.conversation_ref,
        click_request_id=UUID(int=901),
        runtime_hooks=_runtime_hooks(lambda binding: binding == context),
    )

    assert execution.result is not None
    assert execution.result.display_payload == {"text": "formal-result"}
    assert execution.requires_reconciliation is False
    assert len(module.confirm_calls) == int(kind == "action_offer")
    assert len(module.recover_calls) == int(kind == "recovery_control")
    assert len(module.check_calls) == int(kind == "result_check_handle")
    rotated.discard_reconciliation(execution)
    assert store.records == old_records
