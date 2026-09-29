from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from .catalog import StaticAssistantCatalog, UnknownAssistantModuleError
from .contracts import (
    ModuleConfirmationCallV1,
    ModuleRecoveryCallV1,
    ModuleResultCheckCallV1,
    ModuleResultV1,
    NonNullJsonValue,
    PublicControlV1,
)
from .ports import ControlCredentialStore
from .presentation import PreparedPresentation, prepare_fallback, prepare_precomposed

type ControlKind = Literal["action_offer", "recovery_control", "result_check_handle"]
type Clock = Callable[[], datetime]
type NonceFactory = Callable[[], str]
type ConfirmationRefFactory = Callable[[], str]
_CONTROL_TTL_SECONDS = 300


class AssistantControlError(RuntimeError):
    """不携带凭据或私有引用的点击控制错误。"""


class ControlRejectedError(AssistantControlError):
    """凭据无效、失配、过期、已消费或不再绑定当前状态。"""


class ControlStoreUnavailableError(AssistantControlError):
    """外部一次性凭据登记不可用或返回损坏数据。"""


class ControlModuleError(AssistantControlError):
    """模块正式入口失败或返回非法信封。"""


@dataclass(frozen=True, slots=True)
class ControlIssueContext:
    actor_context_ref: str
    conversation_ref: str
    turn_ref: str
    run_ref: str
    generation: int
    module_ref: str
    module_state_ref: str
    state_revision: int
    issuance_request_id: UUID


@dataclass(frozen=True, slots=True)
class ControlRuntimeHooks:
    reserve: Callable[[ControlIssueContext, ControlKind, UUID, int], str]
    retire_consumed: Callable[[str], None]
    attach_reconciliation: Callable[[str, PublicControlV1], None]
    mark_invoked: Callable[[str], None]
    release: Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ControlExecution:
    consumed_binding: ControlIssueContext
    binding: ControlIssueContext
    result: ModuleResultV1 | None
    controls: tuple[PublicControlV1, ...]
    reservation_ref: str
    requires_reconciliation: bool
    reconciliation_credential_ref: str | None = None
    reconciliation_payload: str | None = None


@dataclass(frozen=True, slots=True)
class _PreparedCredential:
    control: PublicControlV1
    credential_ref: str
    payload: str


@dataclass(frozen=True, slots=True)
class _StoredControl:
    version: str
    key_id: str
    kind: ControlKind
    nonce: str
    expires_at: int
    actor_context_ref: str
    conversation_ref: str
    turn_ref: str
    run_ref: str
    generation: int
    module_ref: str
    module_state_ref: str
    state_revision: int
    issuance_request_id: str
    private_ref: str
    confirmation_ref: str | None
    record_signature: str

    def issue_context(self) -> ControlIssueContext:
        return ControlIssueContext(
            actor_context_ref=self.actor_context_ref,
            conversation_ref=self.conversation_ref,
            turn_ref=self.turn_ref,
            run_ref=self.run_ref,
            generation=self.generation,
            module_ref=self.module_ref,
            module_state_ref=self.module_state_ref,
            state_revision=self.state_revision,
            issuance_request_id=UUID(self.issuance_request_id),
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _nonce() -> str:
    return secrets.token_hex(32)


def _confirmation_ref() -> str:
    return f"confirmation-{secrets.token_hex(24)}"


def _is_key_id(value: object) -> bool:
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-"
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 32
        and all(character in allowed for character in value)
    )


def _purpose_key(key: bytes, purpose: bytes) -> bytes:
    return hmac.new(key, b"assistant-control-v1\0" + purpose, hashlib.sha256).digest()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(f"{value}{padding}", altchars=b"-_", validate=True)


class AssistantControlService:
    """只按三种固定控制类型签发、消费并投递模块正式入口。"""

    def __init__(
        self,
        *,
        catalog: StaticAssistantCatalog,
        store: ControlCredentialStore,
        signing_keys: Mapping[str, bytes],
        active_key_id: str,
        clock: Clock | None = None,
        nonce_factory: NonceFactory | None = None,
        confirmation_ref_factory: ConfirmationRefFactory | None = None,
    ) -> None:
        keys = dict(signing_keys)
        if (
            not keys
            or active_key_id not in keys
            or not _is_key_id(active_key_id)
            or any(not _is_key_id(key_id) for key_id in keys)
            or any(len(value) < 32 for value in keys.values())
        ):
            raise ValueError("control signing configuration is invalid")
        self._catalog = catalog
        self._store = store
        self._signing_keys = keys
        self._active_key_id = active_key_id
        self._clock = clock or _utc_now
        self._nonce_factory = nonce_factory or _nonce
        self._confirmation_ref_factory = confirmation_ref_factory or _confirmation_ref

    def issue_from_result(
        self,
        *,
        context: ControlIssueContext,
        result: ModuleResultV1,
    ) -> tuple[PublicControlV1, ...]:
        if (
            result.staged_state_ref != context.module_state_ref
            or result.state_revision != context.state_revision
        ):
            raise ControlRejectedError("control is unavailable")
        private_values = self._private_values(context=context, result=result)
        requested: list[tuple[ControlKind, str, str]] = []
        if result.action_offer is not None:
            requested.append(
                (
                    "action_offer",
                    result.action_offer.action_offer_ref,
                    result.action_offer.safe_label,
                )
            )
        if result.recovery_control is not None:
            requested.append(
                (
                    "recovery_control",
                    result.recovery_control.recovery_control_ref,
                    result.recovery_control.safe_label,
                )
            )
        if result.result_check_handle is not None:
            requested.append(
                ("result_check_handle", result.result_check_handle.result_check_ref, "核验结果")
            )
        public_candidates: tuple[object, ...] = (
            result.display_payload,
            result.display_template,
            result.field_map,
            None
            if result.fallback_presentation is None
            else result.fallback_presentation.display_payload,
            *(label for _kind, _ref, label in requested),
        )
        if any(
            self._contains_private(candidate, private_values) for candidate in public_candidates
        ):
            raise ControlModuleError("control module failed")
        return tuple(
            self._issue(context=context, kind=kind, private_ref=private_ref, safe_label=label)
            for kind, private_ref, label in requested
        )

    def execute(
        self,
        *,
        handle: str,
        expected_kind: ControlKind,
        actor_context_ref: str,
        conversation_ref: str,
        click_request_id: UUID,
        runtime_hooks: ControlRuntimeHooks,
    ) -> ControlExecution:
        token = self._verify_handle(handle, expected_kind)
        credential_ref = self._credential_ref(token["n"])
        result: object
        try:
            raw = self._store.get(credential_ref=credential_ref)
        except Exception:
            raise ControlStoreUnavailableError("control store is unavailable") from None
        if raw is None:
            raise ControlRejectedError("control is unavailable")
        record = self._parse_record(raw)
        if (
            record.version != token["v"]
            or record.key_id != token["k"]
            or record.kind != token["t"]
            or record.nonce != token["n"]
            or record.expires_at != token["e"]
            or record.actor_context_ref != actor_context_ref
            or record.conversation_ref != conversation_ref
            or record.expires_at <= self._now_epoch()
        ):
            raise ControlRejectedError("control is unavailable")
        binding = record.issue_context()
        try:
            entry = self._catalog.resolve(record.module_ref)
        except UnknownAssistantModuleError:
            raise ControlRejectedError("control is unavailable") from None
        try:
            reservation_ref = runtime_hooks.reserve(
                binding,
                record.kind,
                click_request_id,
                record.expires_at,
            )
        except Exception:
            raise ControlRejectedError("control is unavailable") from None
        reconciliation: _PreparedCredential | None = None
        try:
            if record.kind in {"action_offer", "result_check_handle"}:
                reconciliation_private_ref = (
                    record.confirmation_ref if record.kind == "action_offer" else record.private_ref
                )
                if reconciliation_private_ref is None:
                    raise ControlRejectedError("control is unavailable")
                reconciliation_context = replace(
                    binding,
                    issuance_request_id=click_request_id,
                )
                consumed = False
                for _attempt in range(3):
                    reconciliation = self._prepare_credential(
                        context=reconciliation_context,
                        kind="result_check_handle",
                        private_ref=reconciliation_private_ref,
                        safe_label="核验结果",
                    )
                    consumed = self._store.consume_and_put(
                        credential_ref=credential_ref,
                        expected_payload=raw,
                        expires_at=record.expires_at,
                        replacement_credential_ref=reconciliation.credential_ref,
                        replacement_payload=reconciliation.payload,
                        ttl_seconds=_CONTROL_TTL_SECONDS,
                    )
                    if consumed is True:
                        break
                    if consumed is not False:
                        raise ControlStoreUnavailableError("control store is unavailable")
                if consumed is not True:
                    raise ControlRejectedError("control is unavailable")
                if reconciliation is None:
                    raise ControlStoreUnavailableError("control store is unavailable")
            else:
                consumed = self._store.consume(
                    credential_ref=credential_ref,
                    expected_payload=raw,
                    expires_at=record.expires_at,
                )
                if consumed is not True:
                    raise ControlRejectedError("control is unavailable")
            runtime_hooks.retire_consumed(reservation_ref)
            if reconciliation is not None:
                runtime_hooks.attach_reconciliation(
                    reservation_ref,
                    reconciliation.control,
                )
            runtime_hooks.mark_invoked(reservation_ref)
        except (ControlRejectedError, ControlStoreUnavailableError):
            try:
                runtime_hooks.release(reservation_ref)
            except Exception:
                pass
            raise
        except Exception:
            if reconciliation is not None:
                try:
                    self._store.discard(
                        credential_ref=reconciliation.credential_ref,
                        expected_payload=reconciliation.payload,
                    )
                except Exception:
                    pass
            try:
                runtime_hooks.release(reservation_ref)
            except Exception:
                pass
            raise ControlStoreUnavailableError("control store is unavailable") from None
        try:
            if record.kind == "action_offer":
                if record.confirmation_ref is None:
                    raise ControlRejectedError("control is unavailable")
                result = cast(
                    object,
                    entry.module.confirm(
                        ModuleConfirmationCallV1(
                            version="1",
                            module_ref=record.module_ref,
                            conversation_ref=record.conversation_ref,
                            turn_ref=record.turn_ref,
                            run_ref=record.run_ref,
                            actor_context_ref=record.actor_context_ref,
                            action_offer_ref=record.private_ref,
                            module_state_ref=record.module_state_ref,
                            state_revision=record.state_revision,
                            confirmation_ref=record.confirmation_ref,
                        )
                    ),
                )
            elif record.kind == "recovery_control":
                result = cast(
                    object,
                    entry.module.recover(
                        ModuleRecoveryCallV1(
                            version="1",
                            module_ref=record.module_ref,
                            conversation_ref=record.conversation_ref,
                            turn_ref=record.turn_ref,
                            run_ref=record.run_ref,
                            actor_context_ref=record.actor_context_ref,
                            recovery_control_ref=record.private_ref,
                            module_state_ref=record.module_state_ref,
                            state_revision=record.state_revision,
                        )
                    ),
                )
            else:
                result = cast(
                    object,
                    entry.module.check_result(
                        ModuleResultCheckCallV1(
                            version="1",
                            module_ref=record.module_ref,
                            conversation_ref=record.conversation_ref,
                            turn_ref=record.turn_ref,
                            run_ref=record.run_ref,
                            actor_context_ref=record.actor_context_ref,
                            result_check_ref=record.private_ref,
                        )
                    ),
                )
        except Exception:
            return self._uncertain_execution(
                binding=binding,
                reservation_ref=reservation_ref,
                reconciliation=reconciliation,
            )
        if not isinstance(result, ModuleResultV1) or self._result_public_surface_contains_private(
            record=record,
            result=result,
        ):
            return self._uncertain_execution(
                binding=binding,
                reservation_ref=reservation_ref,
                reconciliation=reconciliation,
            )
        if record.kind == "action_offer" and result.result_check_handle is not None:
            return self._uncertain_execution(
                binding=binding,
                reservation_ref=reservation_ref,
                reconciliation=reconciliation,
            )
        next_binding = ControlIssueContext(
            actor_context_ref=record.actor_context_ref,
            conversation_ref=record.conversation_ref,
            turn_ref=record.turn_ref,
            run_ref=record.run_ref,
            generation=record.generation,
            module_ref=record.module_ref,
            module_state_ref=result.staged_state_ref,
            state_revision=result.state_revision,
            issuance_request_id=click_request_id,
        )
        try:
            controls = self.issue_from_result(context=next_binding, result=result)
        except Exception:
            return self._uncertain_execution(
                binding=binding,
                reservation_ref=reservation_ref,
                reconciliation=reconciliation,
            )
        return ControlExecution(
            consumed_binding=binding,
            binding=next_binding,
            result=result,
            controls=controls,
            reservation_ref=reservation_ref,
            requires_reconciliation=False,
            reconciliation_credential_ref=(
                None if reconciliation is None else reconciliation.credential_ref
            ),
            reconciliation_payload=None if reconciliation is None else reconciliation.payload,
        )

    @staticmethod
    def _uncertain_execution(
        *,
        binding: ControlIssueContext,
        reservation_ref: str,
        reconciliation: _PreparedCredential | None,
    ) -> ControlExecution:
        return ControlExecution(
            consumed_binding=binding,
            binding=binding,
            result=None,
            controls=() if reconciliation is None else (reconciliation.control,),
            reservation_ref=reservation_ref,
            requires_reconciliation=True,
            reconciliation_credential_ref=(
                None if reconciliation is None else reconciliation.credential_ref
            ),
            reconciliation_payload=None if reconciliation is None else reconciliation.payload,
        )

    def discard_reconciliation(self, execution: ControlExecution) -> None:
        credential_ref = execution.reconciliation_credential_ref
        payload = execution.reconciliation_payload
        if credential_ref is None or payload is None:
            return
        try:
            self._store.discard(
                credential_ref=credential_ref,
                expected_payload=payload,
            )
        except Exception:
            return

    def prepare_presentation(self, execution: ControlExecution) -> PreparedPresentation:
        """先确定性呈现正式点击结果；模型续答另经原任务续接边界，不在此重执行。"""

        try:
            entry = self._catalog.resolve(execution.binding.module_ref)
        except UnknownAssistantModuleError:
            raise ControlModuleError("control module failed") from None
        result = execution.result
        if result is None:
            raise ControlModuleError("control module failed")
        if result.presentation_mode not in entry.presentation_capabilities:
            if result.fallback_presentation is None:
                raise ControlModuleError("control module failed")
            return prepare_fallback(result.fallback_presentation)
        if result.presentation_mode == "direct":
            if result.display_payload is None:
                raise ControlModuleError("control module failed")
            return PreparedPresentation(
                display_kind=result.display_kind,
                display_payload=cast(NonNullJsonValue, result.display_payload),
            )
        if result.presentation_mode == "precomposed":
            if (
                result.display_template is None
                or result.field_map is None
                or result.fallback_presentation is None
            ):
                raise ControlModuleError("control module failed")
            return prepare_precomposed(
                display_kind=result.display_kind,
                display_template=result.display_template,
                field_map=result.field_map,
                declared_field_names=entry.model_visible_field_names,
                fallback=result.fallback_presentation,
            )
        if result.fallback_presentation is None:
            raise ControlModuleError("control module failed")
        return prepare_fallback(result.fallback_presentation)

    def _issue(
        self,
        *,
        context: ControlIssueContext,
        kind: ControlKind,
        private_ref: str,
        safe_label: str,
    ) -> PublicControlV1:
        for _attempt in range(3):
            prepared = self._prepare_credential(
                context=context,
                kind=kind,
                private_ref=private_ref,
                safe_label=safe_label,
            )
            try:
                stored = self._store.put(
                    credential_ref=prepared.credential_ref,
                    payload=prepared.payload,
                    ttl_seconds=_CONTROL_TTL_SECONDS,
                )
            except Exception:
                raise ControlStoreUnavailableError("control store is unavailable") from None
            if stored is True:
                return prepared.control
            if stored is not False:
                raise ControlStoreUnavailableError("control store is unavailable")
        raise ControlStoreUnavailableError("control store is unavailable")

    def _prepare_credential(
        self,
        *,
        context: ControlIssueContext,
        kind: ControlKind,
        private_ref: str,
        safe_label: str,
    ) -> _PreparedCredential:
        expires_at = self._now_epoch() + _CONTROL_TTL_SECONDS
        nonce = self._nonce_factory()
        if len(nonce) != 64 or any(character not in "0123456789abcdef" for character in nonce):
            raise ValueError("control nonce factory returned an invalid nonce")
        record = _StoredControl(
            version="1",
            key_id=self._active_key_id,
            kind=kind,
            nonce=nonce,
            expires_at=expires_at,
            actor_context_ref=context.actor_context_ref,
            conversation_ref=context.conversation_ref,
            turn_ref=context.turn_ref,
            run_ref=context.run_ref,
            generation=context.generation,
            module_ref=context.module_ref,
            module_state_ref=context.module_state_ref,
            state_revision=context.state_revision,
            issuance_request_id=str(context.issuance_request_id),
            private_ref=private_ref,
            confirmation_ref=self._confirmation_ref_factory() if kind == "action_offer" else None,
            record_signature="",
        )
        record = replace(record, record_signature=self._record_signature(record))
        payload = json.dumps(
            asdict(record), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        return _PreparedCredential(
            control=PublicControlV1(
                kind=kind,
                handle=self._sign_handle(kind=kind, nonce=nonce, expires_at=expires_at),
                safe_label=safe_label,
                expires_at=expires_at,
                manual_review_on_expiry=kind == "result_check_handle",
                module_ref=context.module_ref,
            ),
            credential_ref=self._credential_ref(nonce),
            payload=payload,
        )

    def _sign_handle(self, *, kind: ControlKind, nonce: str, expires_at: int) -> str:
        kind_code = {
            "action_offer": "a",
            "recovery_control": "r",
            "result_check_handle": "c",
        }[kind]
        payload = f"1|{self._active_key_id}|{kind_code}|{expires_at}|{nonce}".encode("ascii")
        signature = hmac.new(
            _purpose_key(self._signing_keys[self._active_key_id], b"public-handle"),
            payload,
            hashlib.sha256,
        ).digest()
        return f"{_encode(payload)}.{_encode(signature)}"

    def _verify_handle(self, handle: object, expected_kind: ControlKind) -> dict[str, object]:
        if not isinstance(handle, str) or not 1 <= len(handle) <= 200 or handle.count(".") != 1:
            raise ControlRejectedError("control is unavailable")
        encoded_payload, encoded_signature = handle.split(".", maxsplit=1)
        try:
            payload = _decode(encoded_payload)
            signature = _decode(encoded_signature)
            version, key_id, kind_code, expires_text, nonce = payload.decode("ascii").split("|")
            expires_at = int(expires_text)
        except (UnicodeDecodeError, ValueError, TypeError):
            raise ControlRejectedError("control is unavailable") from None
        kind = {
            "a": "action_offer",
            "r": "recovery_control",
            "c": "result_check_handle",
        }.get(kind_code)
        if (
            version != "1"
            or kind != expected_kind
            or not expires_text.isascii()
            or not expires_text.isdecimal()
            or len(nonce) != 64
            or any(character not in "0123456789abcdef" for character in nonce)
        ):
            raise ControlRejectedError("control is unavailable")
        key = self._signing_keys.get(key_id)
        if key is None:
            raise ControlRejectedError("control is unavailable")
        expected = hmac.new(_purpose_key(key, b"public-handle"), payload, hashlib.sha256).digest()
        if len(signature) != 32 or not hmac.compare_digest(signature, expected):
            raise ControlRejectedError("control is unavailable")
        if expires_at <= self._now_epoch():
            raise ControlRejectedError("control is unavailable")
        return {"e": expires_at, "k": key_id, "n": nonce, "t": kind, "v": version}

    @staticmethod
    def _credential_ref(nonce: object) -> str:
        if not isinstance(nonce, str) or len(nonce) != 64:
            raise ControlRejectedError("control is unavailable")
        return f"assistant-control:v1:{nonce}"

    def _parse_record(self, raw: object) -> _StoredControl:
        if not isinstance(raw, str) or len(raw) > 8_000:
            raise ControlStoreUnavailableError("control store is unavailable")
        try:
            value = cast(object, json.loads(raw))
            if not isinstance(value, dict):
                raise ValueError
            data = cast(dict[str, object], value)
            if set(data) != set(_StoredControl.__dataclass_fields__):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ControlStoreUnavailableError("control store is unavailable") from None
        version = data["version"]
        key_id = data["key_id"]
        kind = data["kind"]
        nonce = data["nonce"]
        expires_at = data["expires_at"]
        actor_context_ref = data["actor_context_ref"]
        conversation_ref = data["conversation_ref"]
        turn_ref = data["turn_ref"]
        run_ref = data["run_ref"]
        generation = data["generation"]
        module_ref = data["module_ref"]
        module_state_ref = data["module_state_ref"]
        state_revision = data["state_revision"]
        issuance_request_id = data["issuance_request_id"]
        private_ref = data["private_ref"]
        confirmation_ref = data["confirmation_ref"]
        record_signature = data["record_signature"]
        if (
            not isinstance(version, str)
            or version != "1"
            or not _is_key_id(key_id)
            or not isinstance(kind, str)
            or kind not in {"action_offer", "recovery_control", "result_check_handle"}
            or not isinstance(nonce, str)
            or len(nonce) != 64
            or any(character not in "0123456789abcdef" for character in nonce)
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or not isinstance(state_revision, int)
            or isinstance(state_revision, bool)
            or state_revision < 1
            or not isinstance(issuance_request_id, str)
            or not self._is_opaque_ref(actor_context_ref)
            or not self._is_opaque_ref(conversation_ref)
            or not self._is_opaque_ref(turn_ref)
            or not self._is_opaque_ref(run_ref)
            or not self._is_module_ref(module_ref)
            or not self._is_opaque_ref(module_state_ref)
            or not self._is_opaque_ref(private_ref)
            or (kind == "action_offer" and not self._is_opaque_ref(confirmation_ref))
            or (kind != "action_offer" and confirmation_ref is not None)
            or not isinstance(record_signature, str)
            or len(record_signature) != 64
            or any(character not in "0123456789abcdef" for character in record_signature)
        ):
            raise ControlStoreUnavailableError("control store is unavailable")
        try:
            UUID(issuance_request_id)
        except ValueError:
            raise ControlStoreUnavailableError("control store is unavailable") from None
        record = _StoredControl(
            version=version,
            key_id=cast(str, key_id),
            kind=cast(ControlKind, kind),
            nonce=nonce,
            expires_at=expires_at,
            actor_context_ref=cast(str, actor_context_ref),
            conversation_ref=cast(str, conversation_ref),
            turn_ref=cast(str, turn_ref),
            run_ref=cast(str, run_ref),
            generation=generation,
            module_ref=cast(str, module_ref),
            module_state_ref=cast(str, module_state_ref),
            state_revision=state_revision,
            issuance_request_id=issuance_request_id,
            private_ref=cast(str, private_ref),
            confirmation_ref=cast(str | None, confirmation_ref),
            record_signature=record_signature,
        )
        expected_signature = self._record_signature(record)
        if not hmac.compare_digest(record.record_signature, expected_signature):
            raise ControlStoreUnavailableError("control store is unavailable")
        return record

    def _record_signature(self, record: _StoredControl) -> str:
        key = self._signing_keys.get(record.key_id)
        if key is None:
            raise ControlStoreUnavailableError("control store is unavailable")
        data = asdict(record)
        data.pop("record_signature")
        payload = json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hmac.new(_purpose_key(key, b"persisted-record"), payload, hashlib.sha256).hexdigest()

    @staticmethod
    def _is_opaque_ref(value: object) -> bool:
        allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-"
        return (
            isinstance(value, str)
            and 1 <= len(value) <= 200
            and all(character in allowed for character in value)
        )

    @staticmethod
    def _is_module_ref(value: object) -> bool:
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789_.-"
        return (
            isinstance(value, str)
            and 1 <= len(value) <= 120
            and value[0].islower()
            and all(character in allowed for character in value)
        )

    @staticmethod
    def _private_values(
        *,
        context: ControlIssueContext,
        result: ModuleResultV1,
    ) -> tuple[str, ...]:
        values = [
            context.actor_context_ref,
            context.conversation_ref,
            context.turn_ref,
            context.run_ref,
            context.module_state_ref,
            result.staged_state_ref,
        ]
        if result.action_offer is not None:
            values.append(result.action_offer.action_offer_ref)
        if result.recovery_control is not None:
            values.append(result.recovery_control.recovery_control_ref)
        if result.result_check_handle is not None:
            values.append(result.result_check_handle.result_check_ref)
        if result.diagnostic_ref is not None:
            values.append(result.diagnostic_ref)
        return tuple(value for value in values if value)

    @classmethod
    def _contains_private(cls, value: object, private_values: tuple[str, ...]) -> bool:
        if isinstance(value, str):
            return any(private in value for private in private_values)
        if isinstance(value, list | tuple):
            sequence = cast(list[object] | tuple[object, ...], value)
            return any(cls._contains_private(item, private_values) for item in sequence)
        if isinstance(value, dict):
            mapping = cast(dict[object, object], value)
            return any(
                cls._contains_private(key, private_values)
                or cls._contains_private(item, private_values)
                for key, item in mapping.items()
            )
        return False

    @classmethod
    def _result_public_surface_contains_private(
        cls,
        *,
        record: _StoredControl,
        result: ModuleResultV1,
    ) -> bool:
        context = record.issue_context()
        private_values = (
            *cls._private_values(context=context, result=result),
            record.private_ref,
            *(tuple() if record.confirmation_ref is None else (record.confirmation_ref,)),
        )
        public_candidates: tuple[object, ...] = (
            result.display_payload,
            result.display_template,
            result.field_map,
            None
            if result.fallback_presentation is None
            else result.fallback_presentation.display_payload,
            None if result.action_offer is None else result.action_offer.safe_label,
            None if result.recovery_control is None else result.recovery_control.safe_label,
        )
        return any(
            cls._contains_private(candidate, private_values) for candidate in public_candidates
        )

    def _now_epoch(self) -> int:
        try:
            value = cast(object, self._clock())
        except Exception:
            raise ControlStoreUnavailableError("control clock is unavailable") from None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ControlStoreUnavailableError("control clock is unavailable")
        return int(value.timestamp())


__all__ = [
    "AssistantControlError",
    "AssistantControlService",
    "ControlExecution",
    "ControlIssueContext",
    "ControlKind",
    "ControlModuleError",
    "ControlRejectedError",
    "ControlRuntimeHooks",
    "ControlStoreUnavailableError",
    "PublicControlV1",
]
