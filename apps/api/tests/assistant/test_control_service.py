from __future__ import annotations

import base64
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib import import_module
from threading import Lock
from typing import Any, cast
from uuid import UUID

import pytest

from app.platform.assistant.catalog import AssistantModuleCatalogEntry, StaticAssistantCatalog
from app.platform.assistant.contracts import (
    ActionOfferV1,
    CapsuleUnchangedV1,
    ModuleConfirmationCallV1,
    ModuleRecoveryCallV1,
    ModuleResultCheckCallV1,
    ModuleResultV1,
    RecoveryControlV1,
    ResultCheckHandleV1,
    ValidationReceiptV1,
)


class _MemoryControlStore:
    def __init__(self) -> None:
        self.records: dict[str, str] = {}
        self.supersessions: dict[str, str] = {}
        self.ttls: list[int] = []
        self.lock = Lock()
        self.current_epoch: int | None = None

    def put(
        self,
        *,
        credential_ref: str,
        payload: str,
        ttl_seconds: int,
    ) -> bool:
        with self.lock:
            self.ttls.append(ttl_seconds)
            if credential_ref in self.records:
                return False
            self.records[credential_ref] = payload
            return True

    def get(self, *, credential_ref: str) -> str | None:
        with self.lock:
            return self.records.get(credential_ref)

    def consume(self, *, credential_ref: str, expected_payload: str, expires_at: int) -> bool:
        if self.current_epoch is not None and self.current_epoch >= expires_at:
            return False
        with self.lock:
            if self.records.get(credential_ref) != expected_payload:
                return False
            del self.records[credential_ref]
            return True

    def consume_and_put(
        self,
        *,
        credential_ref: str,
        expected_payload: str,
        expires_at: int,
        replacement_credential_ref: str,
        replacement_payload: str,
        ttl_seconds: int,
    ) -> bool:
        if self.current_epoch is not None and self.current_epoch >= expires_at:
            return False
        with self.lock:
            if (
                self.records.get(credential_ref) != expected_payload
                or replacement_credential_ref in self.records
            ):
                return False
            self.ttls.append(ttl_seconds)
            self.records[replacement_credential_ref] = replacement_payload
            del self.records[credential_ref]
            return True

    def discard(self, *, credential_ref: str, expected_payload: str) -> bool:
        with self.lock:
            if self.records.get(credential_ref) != expected_payload:
                return False
            del self.records[credential_ref]
            return True


class _ControlProbeModule:
    def __init__(self) -> None:
        self.confirm_calls: list[ModuleConfirmationCallV1] = []
        self.recover_calls: list[ModuleRecoveryCallV1] = []
        self.check_calls: list[ModuleResultCheckCallV1] = []
        self.confirm_result = _direct_result(
            state_revision=2,
            staged_state_ref="state-private-2",
        )
        self.recover_result = _direct_result(
            state_revision=2,
            staged_state_ref="state-private-2",
        )
        self.check_result_value = _direct_result(
            state_revision=2,
            staged_state_ref="state-private-2",
        )

    def handle(self, request: object) -> ModuleResultV1:
        raise AssertionError(f"handle must not run on a formal click: {request!r}")

    def confirm(self, request: ModuleConfirmationCallV1) -> ModuleResultV1:
        self.confirm_calls.append(request)
        return self.confirm_result

    def recover(self, request: ModuleRecoveryCallV1) -> ModuleResultV1:
        self.recover_calls.append(request)
        return self.recover_result

    def check_result(self, request: ModuleResultCheckCallV1) -> ModuleResultV1:
        self.check_calls.append(request)
        return self.check_result_value


def _direct_result(
    *,
    state_revision: int,
    staged_state_ref: str,
    action_offer: ActionOfferV1 | None = None,
    recovery_control: RecoveryControlV1 | None = None,
    result_check_handle: ResultCheckHandleV1 | None = None,
) -> ModuleResultV1:
    return ModuleResultV1(
        version="1",
        validation_receipt=ValidationReceiptV1(
            status="accepted",
            safe_result_family="fixture.control",
        ),
        locked_notice=None,
        staged_state_ref=staged_state_ref,
        state_revision=state_revision,
        capsule_update=CapsuleUnchangedV1(kind="unchanged"),
        presentation_mode="direct",
        display_kind="text",
        display_payload={"text": "formal-result"},
        display_template=None,
        field_map=None,
        model_safe_narration=None,
        slot_map=None,
        fallback_presentation=None,
        model_safe_note=None,
        action_offer=action_offer,
        recovery_control=recovery_control,
        result_check_handle=result_check_handle,
        safe_error_projection=None,
        diagnostic_ref=None,
    )


def _control_types() -> tuple[type[Any], type[Any]]:
    try:
        module = import_module("app.platform.assistant.controls")
    except ModuleNotFoundError:
        pytest.fail("K6 pure assistant control service is missing")
    return cast(type[Any], module.AssistantControlService), cast(
        type[Any], module.ControlIssueContext
    )


def _runtime_hooks(validator: Any) -> Any:
    controls_module = import_module("app.platform.assistant.controls")

    def reserve(binding: Any, _kind: str, _request_id: UUID, _expires_at: int) -> str:
        if validator(binding) is not True:
            raise RuntimeError("stale binding")
        return "test-reservation"

    return controls_module.ControlRuntimeHooks(
        reserve=reserve,
        retire_consumed=lambda _reservation_ref: None,
        attach_reconciliation=lambda _reservation_ref, _control: None,
        mark_invoked=lambda _reservation_ref: None,
        release=lambda _reservation_ref: None,
    )


def test_action_credential_survives_service_reconstruction_and_calls_confirm_only() -> None:
    service_type, context_type = _control_types()
    store = _MemoryControlStore()
    module = _ControlProbeModule()
    catalog = StaticAssistantCatalog(
        (
            AssistantModuleCatalogEntry(
                module_ref="fixture.control",
                model_safe_capability="固定控制探针。",
                module=module,
            ),
        )
    )
    key = b"k" * 32
    now = datetime(2026, 8, 18, 1, 2, 3, tzinfo=UTC)
    context = context_type(
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
    service = service_type(
        catalog=catalog,
        store=store,
        signing_keys={"active": key},
        active_key_id="active",
        clock=lambda: now,
        nonce_factory=lambda: "a" * 64,
        confirmation_ref_factory=lambda: "confirmation-private-1",
    )

    controls = service.issue_from_result(
        context=context,
        result=_direct_result(
            state_revision=1,
            staged_state_ref="state-private-1",
            action_offer=ActionOfferV1(
                action_offer_ref="offer-private-canary",
                safe_label="确认办理",
            ),
        ),
    )

    assert len(controls) == 1
    public = controls[0]
    assert public.kind == "action_offer"
    assert public.safe_label == "确认办理"
    assert "offer-private-canary" not in public.handle
    assert "state-private-1" not in public.handle
    assert len(store.records) == 1

    rebuilt = service_type(
        catalog=catalog,
        store=store,
        signing_keys={"active": key},
        active_key_id="active",
        clock=lambda: now,
        nonce_factory=lambda: "b" * 64,
        confirmation_ref_factory=lambda: "unused-confirmation",
    )
    execution = rebuilt.execute(
        handle=public.handle,
        expected_kind="action_offer",
        actor_context_ref="actor-private-1",
        conversation_ref="conversation-1",
        click_request_id=UUID(int=900),
        runtime_hooks=_runtime_hooks(lambda binding: binding == context),
    )

    assert execution.result is not None
    assert execution.result.display_payload == {"text": "formal-result"}
    assert len(module.confirm_calls) == 1
    assert module.confirm_calls[0] == ModuleConfirmationCallV1(
        version="1",
        module_ref="fixture.control",
        conversation_ref="conversation-1",
        turn_ref="turn-1",
        run_ref="run-1",
        actor_context_ref="actor-private-1",
        action_offer_ref="offer-private-canary",
        module_state_ref="state-private-1",
        state_revision=1,
        confirmation_ref="confirmation-private-1",
    )
    rebuilt.discard_reconciliation(execution)
    assert store.records == {}


def _service_and_context(
    *,
    nonce: str = "c" * 64,
) -> tuple[Any, Any, _MemoryControlStore, _ControlProbeModule, Any]:
    service_type, context_type = _control_types()
    store = _MemoryControlStore()
    module = _ControlProbeModule()
    catalog = StaticAssistantCatalog(
        (
            AssistantModuleCatalogEntry(
                module_ref="fixture.control",
                model_safe_capability="固定控制探针。",
                module=module,
            ),
        )
    )
    context = context_type(
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
    nonce_count = iter(range(16))
    service = service_type(
        catalog=catalog,
        store=store,
        signing_keys={"active": b"k" * 32},
        active_key_id="active",
        clock=lambda: datetime(2026, 8, 18, 1, 2, 3, tzinfo=UTC),
        nonce_factory=lambda: f"{nonce[:63]}{next(nonce_count):x}",
        confirmation_ref_factory=lambda: "confirmation-private-1",
    )
    return service, context, store, module, service_type


def test_action_control_rejects_tamper_mismatch_stale_missing_and_replay_without_calls() -> None:
    controls_module = import_module("app.platform.assistant.controls")
    rejected = controls_module.ControlRejectedError

    def issued() -> tuple[Any, Any, _MemoryControlStore, _ControlProbeModule, str]:
        service, context, store, module, _service_type = _service_and_context()
        handle = service.issue_from_result(
            context=context,
            result=_direct_result(
                state_revision=1,
                staged_state_ref="state-private-1",
                action_offer=ActionOfferV1(
                    action_offer_ref="offer-private-canary",
                    safe_label="确认办理",
                ),
            ),
        )[0].handle
        return service, context, store, module, handle

    for mutation in (
        "tamper",
        "wrong_kind",
        "wrong_actor",
        "wrong_conversation",
        "stale_revision",
    ):
        service, context, _store, module, handle = issued()
        kwargs = {
            "handle": handle,
            "expected_kind": "action_offer",
            "actor_context_ref": "actor-private-1",
            "conversation_ref": "conversation-1",
            "click_request_id": UUID(int=900),
            "runtime_hooks": _runtime_hooks(lambda binding, expected=context: binding == expected),
        }
        if mutation == "tamper":
            kwargs["handle"] = f"A{handle[1:]}"
        elif mutation == "wrong_kind":
            kwargs["expected_kind"] = "recovery_control"
        elif mutation == "wrong_actor":
            kwargs["actor_context_ref"] = "actor-private-2"
        elif mutation == "wrong_conversation":
            kwargs["conversation_ref"] = "conversation-2"
        else:
            kwargs["runtime_hooks"] = _runtime_hooks(lambda binding: binding.state_revision == 2)
        with pytest.raises(rejected, match="control is unavailable"):
            service.execute(**kwargs)
        assert module.confirm_calls == []

    service, context, store, module, handle = issued()
    store.records.clear()
    with pytest.raises(rejected, match="control is unavailable"):
        service.execute(
            handle=handle,
            expected_kind="action_offer",
            actor_context_ref="actor-private-1",
            conversation_ref="conversation-1",
            click_request_id=UUID(int=900),
            runtime_hooks=_runtime_hooks(lambda binding: binding == context),
        )
    assert module.confirm_calls == []

    malformed_service, _context, _store, malformed_module, _service_type = _service_and_context()
    with pytest.raises(rejected):
        malformed_service.execute(
            handle="malformed-control-handle",
            expected_kind="action_offer",
            actor_context_ref="actor-private-1",
            conversation_ref="conversation-1",
            click_request_id=UUID(int=900),
            runtime_hooks=_runtime_hooks(lambda _binding: False),
        )
    payload = f"2|active|a|1787015223|{'e' * 64}".encode("ascii")
    handle_key = hmac.new(
        b"k" * 32,
        b"assistant-control-v1\0public-handle",
        hashlib.sha256,
    ).digest()
    signature = hmac.new(handle_key, payload, hashlib.sha256).digest()
    encoded_payload = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    with pytest.raises(rejected):
        malformed_service.execute(
            handle=f"{encoded_payload}.{encoded_signature}",
            expected_kind="action_offer",
            actor_context_ref="actor-private-1",
            conversation_ref="conversation-1",
            click_request_id=UUID(int=900),
            runtime_hooks=_runtime_hooks(lambda _binding: True),
        )
    assert malformed_module.confirm_calls == []

    service, context, _store, module, handle = issued()
    service.execute(
        handle=handle,
        expected_kind="action_offer",
        actor_context_ref="actor-private-1",
        conversation_ref="conversation-1",
        click_request_id=UUID(int=900),
        runtime_hooks=_runtime_hooks(lambda binding: binding == context),
    )
    with pytest.raises(rejected, match="control is unavailable"):
        service.execute(
            handle=handle,
            expected_kind="action_offer",
            actor_context_ref="actor-private-1",
            conversation_ref="conversation-1",
            click_request_id=UUID(int=901),
            runtime_hooks=_runtime_hooks(lambda binding: binding == context),
        )
    assert len(module.confirm_calls) == 1


def test_two_concurrent_action_clicks_have_one_winner_and_one_module_call() -> None:
    controls_module = import_module("app.platform.assistant.controls")
    service, context, _store, module, _service_type = _service_and_context()
    handle = service.issue_from_result(
        context=context,
        result=_direct_result(
            state_revision=1,
            staged_state_ref="state-private-1",
            action_offer=ActionOfferV1(
                action_offer_ref="offer-private-canary",
                safe_label="确认办理",
            ),
        ),
    )[0].handle

    def click() -> str:
        try:
            service.execute(
                handle=handle,
                expected_kind="action_offer",
                actor_context_ref="actor-private-1",
                conversation_ref="conversation-1",
                click_request_id=UUID(int=900),
                runtime_hooks=_runtime_hooks(lambda binding: binding == context),
            )
        except controls_module.ControlRejectedError:
            return "rejected"
        return "executed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: click(), range(2)))

    assert sorted(outcomes) == ["executed", "rejected"]
    assert len(module.confirm_calls) == 1


def test_new_recovery_revision_invalidates_old_control_and_calls_recover_only() -> None:
    controls_module = import_module("app.platform.assistant.controls")
    service, context, _store, module, _service_type = _service_and_context()
    old_handle = service.issue_from_result(
        context=context,
        result=_direct_result(
            state_revision=1,
            staged_state_ref="state-private-1",
            recovery_control=RecoveryControlV1(
                recovery_control_ref="recovery-private-1",
                safe_label="放弃当前内容",
            ),
        ),
    )[0].handle
    newer = type(context)(
        actor_context_ref=context.actor_context_ref,
        conversation_ref=context.conversation_ref,
        turn_ref="turn-2",
        run_ref="run-2",
        generation=2,
        module_ref=context.module_ref,
        module_state_ref="state-private-2",
        state_revision=2,
        issuance_request_id=UUID(int=102),
    )
    new_handle = service.issue_from_result(
        context=newer,
        result=_direct_result(
            state_revision=2,
            staged_state_ref="state-private-2",
            recovery_control=RecoveryControlV1(
                recovery_control_ref="recovery-private-2",
                safe_label="放弃当前内容",
            ),
        ),
    )[0].handle

    with pytest.raises(controls_module.ControlRejectedError):
        service.execute(
            handle=old_handle,
            expected_kind="recovery_control",
            actor_context_ref=context.actor_context_ref,
            conversation_ref=context.conversation_ref,
            click_request_id=UUID(int=900),
            runtime_hooks=_runtime_hooks(lambda _binding: False),
        )
    service.execute(
        handle=new_handle,
        expected_kind="recovery_control",
        actor_context_ref=context.actor_context_ref,
        conversation_ref=context.conversation_ref,
        click_request_id=UUID(int=901),
        runtime_hooks=_runtime_hooks(lambda binding: binding == newer),
    )
    assert len(module.recover_calls) == 1
    assert module.confirm_calls == []
    assert module.check_calls == []


def test_result_check_unknown_issues_fresh_handle_and_never_calls_confirm() -> None:
    controls_module = import_module("app.platform.assistant.controls")
    service, context, _store, module, _service_type = _service_and_context(nonce="d" * 64)
    module.check_result_value = _direct_result(
        state_revision=2,
        staged_state_ref="state-private-2",
        result_check_handle=ResultCheckHandleV1(result_check_ref="result-private-2"),
    )
    old_handle = service.issue_from_result(
        context=context,
        result=_direct_result(
            state_revision=1,
            staged_state_ref="state-private-1",
            result_check_handle=ResultCheckHandleV1(result_check_ref="result-private-1"),
        ),
    )[0].handle
    execution = service.execute(
        handle=old_handle,
        expected_kind="result_check_handle",
        actor_context_ref=context.actor_context_ref,
        conversation_ref=context.conversation_ref,
        click_request_id=UUID(int=900),
        runtime_hooks=_runtime_hooks(lambda binding: binding == context),
    )

    assert len(execution.controls) == 1
    assert execution.controls[0].kind == "result_check_handle"
    assert execution.controls[0].handle != old_handle
    assert len(module.check_calls) == 1
    assert module.confirm_calls == []
    with pytest.raises(controls_module.ControlRejectedError):
        service.execute(
            handle=old_handle,
            expected_kind="result_check_handle",
            actor_context_ref=context.actor_context_ref,
            conversation_ref=context.conversation_ref,
            click_request_id=UUID(int=901),
            runtime_hooks=_runtime_hooks(lambda _binding: True),
        )
    assert len(module.check_calls) == 1


def test_private_control_refs_cannot_enter_public_labels_or_formal_result_payloads() -> None:
    controls_module = import_module("app.platform.assistant.controls")
    service, context, store, module, _service_type = _service_and_context()

    with pytest.raises(controls_module.ControlModuleError) as unsafe_label:
        service.issue_from_result(
            context=context,
            result=_direct_result(
                state_revision=1,
                staged_state_ref="state-private-1",
                action_offer=ActionOfferV1(
                    action_offer_ref="offer-private-canary",
                    safe_label="确认 offer-private-canary",
                ),
            ),
        )
    assert "offer-private-canary" not in str(unsafe_label.value)
    assert store.records == {}

    unsafe_initial_result = _direct_result(
        state_revision=1,
        staged_state_ref="state-private-1",
        action_offer=ActionOfferV1(
            action_offer_ref="offer-private-canary",
            safe_label="确认办理",
        ),
    ).model_copy(update={"display_payload": {"text": "offer-private-canary"}})
    with pytest.raises(controls_module.ControlModuleError) as unsafe_initial:
        service.issue_from_result(context=context, result=unsafe_initial_result)
    assert "offer-private-canary" not in str(unsafe_initial.value)
    assert store.records == {}

    handle = service.issue_from_result(
        context=context,
        result=_direct_result(
            state_revision=1,
            staged_state_ref="state-private-1",
            action_offer=ActionOfferV1(
                action_offer_ref="offer-private-canary",
                safe_label="确认办理",
            ),
        ),
    )[0].handle
    module.confirm_result = _direct_result(
        state_revision=2,
        staged_state_ref="state-private-2",
    ).model_copy(update={"display_payload": {"text": "offer-private-canary"}})

    unsafe_execution = service.execute(
        handle=handle,
        expected_kind="action_offer",
        actor_context_ref=context.actor_context_ref,
        conversation_ref=context.conversation_ref,
        click_request_id=UUID(int=900),
        runtime_hooks=_runtime_hooks(lambda binding: binding == context),
    )
    assert unsafe_execution.requires_reconciliation is True
    assert unsafe_execution.result is None
    assert len(unsafe_execution.controls) == 1
    assert "offer-private-canary" not in unsafe_execution.controls[0].handle
    assert len(module.confirm_calls) == 1
    assert len(store.records) == 1


def test_expired_unknown_key_wrong_catalog_and_corrupt_store_fail_before_module_call() -> None:
    controls_module = import_module("app.platform.assistant.controls")
    service, context, store, module, service_type = _service_and_context()
    handle = service.issue_from_result(
        context=context,
        result=_direct_result(
            state_revision=1,
            staged_state_ref="state-private-1",
            action_offer=ActionOfferV1(
                action_offer_ref="offer-private-canary",
                safe_label="确认办理",
            ),
        ),
    )[0].handle

    expired = service_type(
        catalog=service._catalog,
        store=store,
        signing_keys={"active": b"k" * 32},
        active_key_id="active",
        clock=lambda: datetime(2026, 8, 18, 1, 20, 3, tzinfo=UTC),
    )
    with pytest.raises(controls_module.ControlRejectedError):
        expired.execute(
            handle=handle,
            expected_kind="action_offer",
            actor_context_ref=context.actor_context_ref,
            conversation_ref=context.conversation_ref,
            click_request_id=UUID(int=900),
            runtime_hooks=_runtime_hooks(lambda _binding: True),
        )

    unknown_key = service_type(
        catalog=service._catalog,
        store=store,
        signing_keys={"rotated": b"r" * 32},
        active_key_id="rotated",
        clock=lambda: datetime(2026, 8, 18, 1, 2, 3, tzinfo=UTC),
    )
    with pytest.raises(controls_module.ControlRejectedError):
        unknown_key.execute(
            handle=handle,
            expected_kind="action_offer",
            actor_context_ref=context.actor_context_ref,
            conversation_ref=context.conversation_ref,
            click_request_id=UUID(int=900),
            runtime_hooks=_runtime_hooks(lambda _binding: True),
        )

    wrong_catalog = service_type(
        catalog=StaticAssistantCatalog(
            (
                AssistantModuleCatalogEntry(
                    module_ref="fixture.other",
                    model_safe_capability="另一个已安装探针。",
                    module=_ControlProbeModule(),
                ),
            )
        ),
        store=store,
        signing_keys={"active": b"k" * 32},
        active_key_id="active",
        clock=lambda: datetime(2026, 8, 18, 1, 2, 3, tzinfo=UTC),
    )
    with pytest.raises(controls_module.ControlRejectedError):
        wrong_catalog.execute(
            handle=handle,
            expected_kind="action_offer",
            actor_context_ref=context.actor_context_ref,
            conversation_ref=context.conversation_ref,
            click_request_id=UUID(int=900),
            runtime_hooks=_runtime_hooks(lambda _binding: True),
        )

    credential_ref, raw = next(iter(store.records.items()))
    corrupt = json.loads(raw)
    corrupt["private_ref"] = ["CORRUPT-PRIVATE-CANARY"]
    store.records[credential_ref] = json.dumps(corrupt)
    with pytest.raises(controls_module.ControlStoreUnavailableError) as failure:
        service.execute(
            handle=handle,
            expected_kind="action_offer",
            actor_context_ref=context.actor_context_ref,
            conversation_ref=context.conversation_ref,
            click_request_id=UUID(int=900),
            runtime_hooks=_runtime_hooks(lambda _binding: True),
        )
    assert "CORRUPT-PRIVATE-CANARY" not in str(failure.value)
    assert module.confirm_calls == []


def test_valid_shaped_persisted_binding_tamper_fails_closed_before_module_call() -> None:
    controls_module = import_module("app.platform.assistant.controls")
    service, context, store, module, _service_type = _service_and_context()
    handle = service.issue_from_result(
        context=context,
        result=_direct_result(
            state_revision=1,
            staged_state_ref="state-private-1",
            action_offer=ActionOfferV1(
                action_offer_ref="offer-private-canary",
                safe_label="确认办理",
            ),
        ),
    )[0].handle
    credential_ref, raw = next(iter(store.records.items()))
    corrupt = json.loads(raw)
    corrupt["private_ref"] = "other-valid-private-ref"
    store.records[credential_ref] = json.dumps(corrupt, separators=(",", ":"), sort_keys=True)

    with pytest.raises(controls_module.ControlStoreUnavailableError):
        service.execute(
            handle=handle,
            expected_kind="action_offer",
            actor_context_ref=context.actor_context_ref,
            conversation_ref=context.conversation_ref,
            click_request_id=UUID(int=900),
            runtime_hooks=_runtime_hooks(lambda binding: binding == context),
        )

    assert module.confirm_calls == []


def test_atomic_store_expiry_crossing_after_reservation_never_calls_module() -> None:
    controls_module = import_module("app.platform.assistant.controls")
    service, context, store, module, _service_type = _service_and_context()
    action = service.issue_from_result(
        context=context,
        result=_direct_result(
            state_revision=1,
            staged_state_ref="state-private-1",
            action_offer=ActionOfferV1(
                action_offer_ref="offer-private-canary",
                safe_label="确认办理",
            ),
        ),
    )[0]
    released: list[str] = []

    def reserve(
        _binding: Any,
        _kind: str,
        _request_id: UUID,
        expires_at: int,
    ) -> str:
        store.current_epoch = expires_at
        return "reservation-boundary"

    with pytest.raises(controls_module.ControlRejectedError):
        service.execute(
            handle=action.handle,
            expected_kind="action_offer",
            actor_context_ref=context.actor_context_ref,
            conversation_ref=context.conversation_ref,
            click_request_id=UUID(int=104),
            runtime_hooks=controls_module.ControlRuntimeHooks(
                reserve=reserve,
                retire_consumed=lambda _reservation_ref: None,
                attach_reconciliation=lambda _reservation_ref, _control: None,
                mark_invoked=lambda _reservation_ref: None,
                release=lambda reservation_ref: released.append(reservation_ref),
            ),
        )

    assert len(store.records) == 1
    assert module.confirm_calls == []
    assert released == ["reservation-boundary"]


def test_action_prebuilds_reconciliation_and_checks_confirmation_ref_only() -> None:
    controls_module = import_module("app.platform.assistant.controls")
    service, context, store, module, service_type = _service_and_context(nonce="e" * 64)
    module.confirm = cast(Any, lambda request: (_ for _ in ()).throw(RuntimeError("AFTER-CALL")))
    handle = service.issue_from_result(
        context=context,
        result=_direct_result(
            state_revision=1,
            staged_state_ref="state-private-1",
            action_offer=ActionOfferV1(
                action_offer_ref="offer-private-canary",
                safe_label="确认办理",
            ),
        ),
    )[0].handle
    attached: list[Any] = []
    invoked: list[str] = []
    released: list[str] = []
    hooks = controls_module.ControlRuntimeHooks(
        reserve=lambda _binding, _kind, _request_id, _expires_at: "reservation-1",
        retire_consumed=lambda _reservation_ref: None,
        attach_reconciliation=lambda _reservation_ref, control: attached.append(control),
        mark_invoked=lambda reservation_ref: invoked.append(reservation_ref),
        release=lambda reservation_ref: released.append(reservation_ref),
    )

    uncertain = service.execute(
        handle=handle,
        expected_kind="action_offer",
        actor_context_ref=context.actor_context_ref,
        conversation_ref=context.conversation_ref,
        click_request_id=UUID(int=102),
        runtime_hooks=hooks,
    )

    assert uncertain.requires_reconciliation is True
    assert uncertain.result is None
    assert uncertain.controls == tuple(attached)
    assert uncertain.controls[0].kind == "result_check_handle"
    assert uncertain.controls[0].expires_at == int(
        datetime(2026, 8, 18, 1, 7, 3, tzinfo=UTC).timestamp()
    )
    assert uncertain.controls[0].manual_review_on_expiry is True
    assert invoked == ["reservation-1"]
    assert released == []
    assert store.ttls == [300, 300]
    assert len(store.records) == 1

    rebuilt_module = _ControlProbeModule()
    rebuilt = service_type(
        catalog=StaticAssistantCatalog(
            (
                AssistantModuleCatalogEntry(
                    module_ref="fixture.control",
                    model_safe_capability="固定控制探针。",
                    module=rebuilt_module,
                ),
            )
        ),
        store=store,
        signing_keys={"active": b"k" * 32},
        active_key_id="active",
        clock=lambda: datetime(2026, 8, 18, 1, 2, 3, tzinfo=UTC),
        nonce_factory=lambda: "f" * 64,
    )
    checked = rebuilt.execute(
        handle=uncertain.controls[0].handle,
        expected_kind="result_check_handle",
        actor_context_ref=context.actor_context_ref,
        conversation_ref=context.conversation_ref,
        click_request_id=UUID(int=103),
        runtime_hooks=controls_module.ControlRuntimeHooks(
            reserve=lambda _binding, _kind, _request_id, _expires_at: "reservation-2",
            retire_consumed=lambda _reservation_ref: None,
            attach_reconciliation=lambda _reservation_ref, _control: None,
            mark_invoked=lambda _reservation_ref: None,
            release=lambda _reservation_ref: None,
        ),
    )

    assert checked.requires_reconciliation is False
    assert len(rebuilt_module.check_calls) == 1
    assert rebuilt_module.check_calls[0].result_check_ref == "confirmation-private-1"
    assert rebuilt_module.confirm_calls == []

    with pytest.raises(TypeError):
        service_type(
            catalog=service._catalog,
            store=store,
            signing_keys={"active": b"k" * 32},
            active_key_id="active",
            ttl_seconds=301,
        )
