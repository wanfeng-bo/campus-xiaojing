# pyright: strict
from __future__ import annotations

import hashlib
import math
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_read_evidence import current_validation_batch
from app.platform.contracts import ActorContext, GuardianActorContext, StaffActorContext
from app.platform.identity.context import CurrentUser
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import (
    ActorContextResolver,
    ContextNoLongerValid,
    ContextResolutionUnavailable,
    SelectedContextIdentity,
    SelectedContextRef,
)
from app.platform.identity.models import User
from app.platform.identity.session import IdentitySession, IdentitySessionStore, SessionRedisClient

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
SESSION_TTL_SECONDS = 28800
_SELECTION_CONTEXT_WINDOW = timedelta(seconds=60)
_REPLACE_SELECTION = """
if (redis.call('GET', KEYS[1]) or '') ~= ARGV[1]
   or redis.call('EXISTS', KEYS[2]) == 0 then return 0 end
redis.call('SETEX', KEYS[1], ARGV[2], ARGV[3])
return 1
"""


@runtime_checkable
class _ProductIdentityRedis(SessionRedisClient, Protocol):
    def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> object: ...


@dataclass(frozen=True, slots=True)
class EngineeringAccount:
    ref: str
    label: str
    external_subject: str
    provider_code: str = "local_test"
    provider_tenant_key: str = "development"


ENGINEERING_ACCOUNTS = (
    EngineeringAccount("maintainer", "合成工程维护员", "f1-maintainer"),
    EngineeringAccount("reader", "合成工程查询员", "f1-reader"),
    EngineeringAccount("unprivileged", "合成工程无维护职责账号", "f1-unprivileged"),
)


FAMILY_ENGINEERING_ACCOUNTS = (
    EngineeringAccount("guardian", "合成工程监护人", "f3-guardian"),
    EngineeringAccount("caregiver", "合成工程认可照护人", "f3-caregiver"),
    EngineeringAccount("teacher-parent", "合成工程教师兼家长", "f3-teacher-parent"),
    EngineeringAccount("unverified-contact", "合成工程未核验联系人", "f3-unverified-contact"),
    EngineeringAccount("second-guardian", "合成工程另一监护人", "f3-second-guardian"),
    EngineeringAccount("verifier", "合成工程关系核验员", "f3-verifier"),
)


class ProductIdentityRejected(RuntimeError):
    """No valid explicitly selected engineering identity; no internal diagnostic payload."""


class _SelectionChanged(ProductIdentityRejected):
    """A concurrent selection changed; the login session may still be valid."""


class ProductIdentityUnavailable(RuntimeError):
    """Current identity cannot be revalidated because its authority is unavailable."""


class ProductSelectionEvent(BaseModel):
    """A dated UI action, never a new grant or a claim about the current turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["guardian_selection_changed"] = "guardian_selection_changed"
    occurred_at: AwareDatetime


@dataclass(frozen=True, slots=True)
class ProductIdentity:
    actor: ActorContext
    session_binding: str = field(repr=False)
    actor_context_ref: str = field(repr=False)
    display_label: str | None = field(default=None, repr=False)
    selection_event: ProductSelectionEvent | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ProductFamilyActor:
    """Conversation owner only; never accepted as a domain or database actor."""

    account_user_id: int
    school_id: UUID
    person_id: UUID
    authorization_version: int


@dataclass(frozen=True, slots=True)
class ProductFamilyIdentity:
    actor: ProductFamilyActor
    session_binding: str = field(repr=False)
    actor_context_ref: str = field(repr=False)


class FamilyChildChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    child_ref: str
    label: str
    is_default: bool = False


class _FamilyScopeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_token: str = Field(repr=False)
    school_id: UUID
    person_id: UUID


class _FamilyChildBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scope_ref: str = Field(repr=False)
    child_ref: str
    private_ref: str = Field(repr=False)
    selected_identity: SelectedContextIdentity = Field(repr=False)
    label: str


@dataclass(frozen=True, slots=True, repr=False)
class ProductIdentityEvidence:
    """Private same-release identity proof, owned and cleared by the release batch."""

    identity: ProductIdentity
    fresh: Callable[[], ProductIdentity]

    def revalidate(self) -> None:
        # Dataclass equality includes the entire actor, session binding and
        # private reference, including any fields later added to the contract.
        if self.fresh() != self.identity:
            raise ProductIdentityRejected("本次助手身份已经变化。")


class IdentityChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ref: str
    label: str
    kind: Literal["staff_assignment", "guardian_relationship"] | None = None


class ProductSessionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["signed_out", "signed_in"]
    accounts: tuple[IdentityChoice, ...]
    contexts: tuple[IdentityChoice, ...] = ()
    selected_context_ref: str | None = None
    can_maintain: bool = False
    directory_enabled: bool = False
    family_enabled: bool = False
    education_enabled: bool = False
    selected_context_kind: Literal["staff_assignment", "guardian_relationship"] | None = None
    family_conversation_available: bool = False
    assistant_context_key: str | None = None
    # Issued only by the successful context-change response; a page reload never retrieves it.
    selection_transition_ref: str | None = Field(default=None, repr=False)


class _PrivateBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_token: str = Field(repr=False)
    context_ref: str
    family_child_ref: str | None = None


class _SelectedState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contexts: tuple[IdentityChoice, ...]
    effective_until: datetime
    selected_context_ref: str | None = None
    private_ref: str | None = None
    selected_identity: SelectedContextIdentity | None = Field(default=None, repr=False)
    selection_event: ProductSelectionEvent | None = Field(default=None, repr=False)
    selection_context_claim: str | None = Field(default=None, repr=False)
    selection_context_conversation_ref: str | None = Field(default=None, repr=False)
    selection_context_claimed_at: AwareDatetime | None = Field(default=None, repr=False)
    family_private_ref: str | None = Field(default=None, repr=False)


class ProductActorContextResolver(Protocol):
    def list_options(
        self, *, exclude_selected: SelectedContextIdentity | None = None
    ) -> tuple[SelectedContextRef, ...]: ...
    def resolve(self, opaque_ref: str) -> object: ...
    def resolve_selected(self, selected: SelectedContextIdentity) -> object: ...


class SchoolFactIdentitySessionService:
    """Shared product selection, conversation, revalidation and revocation mechanics."""

    def __init__(
        self, *, redis: SessionRedisClient,
        resolver_factory: Callable[[CurrentUser, IdentitySession], ProductActorContextResolver],
        can_maintain: Callable[[StaffActorContext], bool], family_enabled: bool = False,
        ttl_seconds: int = SESSION_TTL_SECONDS,
    ) -> None:
        if not isinstance(redis, _ProductIdentityRedis):
            raise ValueError("product identity requires atomic Redis selection replacement")
        self._accounts: dict[str, EngineeringAccount] = {}
        self._redis = redis
        self._store = IdentitySessionStore(redis=redis, ttl_seconds=ttl_seconds)
        self._resolver_factory = resolver_factory
        self._can_maintain = can_maintain
        self._family_enabled = family_enabled

    def _complete(self, token: str | None) -> tuple[CurrentUser, IdentitySession]:
        raise NotImplementedError

    def logout(self, token: str | None) -> None:
        if not token:
            return
        try:
            identity = self._store.read(token)
            if identity is not None:
                raw = self._get(self._state_key(identity))
                state = self._parse_state(raw)
                if state is not None and state.private_ref is not None:
                    self._redis.delete(self._private_key(state.private_ref))
                self._redis.delete(self._state_key(identity))
            self._store.delete(token)
        except RedisError as exc:
            raise ProductIdentityUnavailable("工程身份服务暂时不可用。") from exc

    def session_view(self, token: str | None) -> ProductSessionView:
        accounts = tuple(IdentityChoice(ref=a.ref, label=a.label) for a in self._accounts.values())
        if not token:
            return ProductSessionView(
                status="signed_out", accounts=accounts, family_enabled=self._family_enabled
            )
        try:
            user, identity = self._complete(token)
            state = self._state(token, user, identity)
            maintained = False
            selected_kind = None
            if state.selected_context_ref is not None:
                try:
                    actor = self._resolve_selection(user, identity, state)
                    maintained = (
                        bool(self._can_maintain(actor))
                        if type(actor) is StaffActorContext
                        else False
                    )
                    selected_kind = (
                        "staff_assignment"
                        if type(actor) is StaffActorContext
                        else "guardian_relationship"
                    )
                except (ContextNoLongerValid, ProductIdentityRejected):
                    if state.family_private_ref and state.selected_identity is not None \
                            and state.selected_identity.context_kind == "guardian":
                        # An expired optional page focus is not the family's authority.
                        # Individual target reads still revalidate their own relationship.
                        self.resolve_assistant_identity(state.family_private_ref)
                        selected_kind = "guardian_relationship"
                    else:
                        state = self._new_options(token, user, identity, previous=state)
            if state.effective_until <= datetime.now(UTC):
                state = self._new_options(token, user, identity, previous=state, preserve=True)
            if selected_kind == "guardian_relationship" and state.family_private_ref is None:
                self.resolve_assistant_request(token)
                state = self._current_state(identity)
            self._require_current_selection(token, identity, state)
            assistant_reference = (
                state.family_private_ref
                if selected_kind == "guardian_relationship"
                else state.private_ref
            )
            return ProductSessionView(
                status="signed_in",
                accounts=accounts,
                contexts=state.contexts,
                selected_context_ref=state.selected_context_ref,
                can_maintain=maintained,
                family_enabled=self._family_enabled,
                selected_context_kind=selected_kind,
                family_conversation_available=(
                    selected_kind == "guardian_relationship"
                ),
                assistant_context_key=(
                    hashlib.sha256(("display-only:" + assistant_reference).encode()).hexdigest()
                    if assistant_reference is not None else None
                ),
            )
        except _SelectionChanged as exc:
            raise ProductIdentityUnavailable("履职选择正在更新，请重试。") from exc
        except ProductIdentityRejected:
            return ProductSessionView(
                status="signed_out", accounts=accounts, family_enabled=self._family_enabled
            )
        except (RedisError, SQLAlchemyError, ContextResolutionUnavailable) as exc:
            raise ProductIdentityUnavailable("工程身份服务暂时不可用。") from exc

    def select_context(self, token: str | None, context_ref: str) -> str | None:
        user, identity = self._complete(token)
        assert token is not None
        state = self._state(token, user, identity)
        if context_ref not in {choice.ref for choice in state.contexts}:
            raise ProductIdentityRejected("请选择本会话当前列出的履职身份。")
        if context_ref == state.selected_context_ref and state.selected_identity is not None:
            self._resolve_selection(user, identity, state)
            self._require_current_selection(token, identity, state)
            return None
        actor = self._resolve(user, identity, context_ref)
        selection_event = (
            ProductSelectionEvent(occurred_at=datetime.now(UTC))
            if type(actor) is GuardianActorContext
            and state.selected_identity is not None
            and state.selected_identity.context_kind == "guardian"
            else None
        )
        selection_context_claim = secrets.token_urlsafe(32) if selection_event else None
        private_ref = secrets.token_urlsafe(32)
        same_family = (
            type(actor) is GuardianActorContext
            and state.selected_identity is not None
            and state.selected_identity.context_kind == "guardian"
            and state.selected_identity.school_id == actor.school_id
            and state.selected_identity.person_id == actor.person_id
        )
        try:
            self._redis.setex(
                self._private_key(private_ref),
                self._ttl(identity),
                _PrivateBinding(session_token=token, context_ref=context_ref).model_dump_json(),
            )
            selected = state.model_copy(
                update={
                    "selected_context_ref": context_ref,
                    "private_ref": private_ref,
                    "selected_identity": SelectedContextIdentity.from_actor(actor, identity),
                    "selection_event": selection_event,
                    "selection_context_claim": selection_context_claim,
                    "selection_context_conversation_ref": None,
                    "selection_context_claimed_at": None,
                    "family_private_ref": state.family_private_ref if same_family else None,
                }
            )
            if not self._replace_state(token, identity, state, selected):
                self._redis.delete(self._private_key(private_ref))
                raise _SelectionChanged("履职选择已经变化，请重新打开当前身份。")
            if state.private_ref:
                self._redis.delete(self._private_key(state.private_ref))
            return selection_context_claim
        except RedisError as exc:
            raise ProductIdentityUnavailable("工程身份服务暂时不可用。") from exc

    def bind_selection_context(
        self, actor_context_ref: str, claim: str, conversation_ref: str
    ) -> bool:
        """Claim the current explicit switch for one new in-memory conversation.

        The claim only supplies dialogue context. It never chooses a child or grants
        a read; both remain tied to the freshly resolved selected guardian context.
        """
        if (
            type(claim) is not str or _TOKEN.fullmatch(claim) is None
            or type(conversation_ref) is not str
            or _TOKEN.fullmatch(conversation_ref) is None
        ):
            return False
        selected = self._resolve_private_actor(actor_context_ref)
        if type(selected.actor) is not GuardianActorContext:
            return False
        raw_binding = self._get(self._private_key(actor_context_ref))
        if not isinstance(raw_binding, str):
            return False
        try:
            binding = _PrivateBinding.model_validate_json(raw_binding)
        except ValidationError:
            return False
        user, session = self._complete(binding.session_token)
        state = self._state(binding.session_token, user, session)
        now = datetime.now(UTC)
        event = state.selection_event
        if (
            state.private_ref != actor_context_ref
            or state.selection_context_claim != claim
            or state.selection_context_conversation_ref is not None
            or event is None
            or selected.selection_event != event
            or not timedelta(0) <= now - event.occurred_at <= _SELECTION_CONTEXT_WINDOW
        ):
            return False
        claimed = state.model_copy(update={
            "selection_context_claim": None,
            "selection_context_conversation_ref": conversation_ref,
            "selection_context_claimed_at": now,
        })
        return self._replace_state(binding.session_token, session, state, claimed)

    def recent_selection_for_turn(
        self, actor_context_ref: str, conversation_ref: str, generation: int
    ) -> bool:
        if generation != 1 or type(conversation_ref) is not str:
            return False
        selected = self._resolve_private_actor(actor_context_ref)
        if type(selected.actor) is not GuardianActorContext:
            return False
        raw_binding = self._get(self._private_key(actor_context_ref))
        if not isinstance(raw_binding, str):
            return False
        try:
            binding = _PrivateBinding.model_validate_json(raw_binding)
        except ValidationError:
            return False
        user, session = self._complete(binding.session_token)
        state = self._state(binding.session_token, user, session)
        claimed_at = state.selection_context_claimed_at
        event = state.selection_event
        now = datetime.now(UTC)
        return (
            state.private_ref == actor_context_ref
            and state.selection_context_conversation_ref == conversation_ref
            and claimed_at is not None
            and event is not None
            and event == selected.selection_event
            and timedelta(0) <= now - event.occurred_at <= _SELECTION_CONTEXT_WINDOW
            and timedelta(0) <= now - claimed_at <= _SELECTION_CONTEXT_WINDOW
        )

    def resolve_assistant_request(
        self, token: str | None
    ) -> ProductIdentity | ProductFamilyIdentity:
        """A family dialogue belongs to the parent; each tool still gets one child actor."""
        user, session = self._complete(token)
        assert token is not None
        state = self._state(token, user, session)
        selected = state.selected_identity
        if selected is None or selected.context_kind != "guardian":
            return self.resolve_request(token)
        if not self._family_enabled:
            raise ProductIdentityRejected("当前未启用家校查询。")
        if state.family_private_ref is None:
            # The first entry requires the actually selected, current relationship.
            self._resolve_selection(user, session, state)
            reference = secrets.token_urlsafe(32)
            self._set_private(
                self._family_scope_key(reference), self._ttl(session),
                _FamilyScopeBinding(session_token=token, school_id=selected.school_id,
                                    person_id=selected.person_id).model_dump_json(),
            )
            updated = state.model_copy(update={"family_private_ref": reference})
            if not self._replace_state(token, session, state, updated):
                self._redis.delete(self._family_scope_key(reference))
                state = self._current_state(session)
                if state.family_private_ref is None:
                    raise _SelectionChanged("当前身份已经变化。")
            else:
                state = updated
        assert state.family_private_ref is not None
        return self.resolve_assistant_identity(state.family_private_ref)

    def resolve_assistant_identity(
        self, reference: str
    ) -> ProductIdentity | ProductFamilyIdentity:
        if type(reference) is not str or _TOKEN.fullmatch(reference) is None:
            raise ProductIdentityRejected("本次助手身份已经失效。")
        raw = self._get(self._family_scope_key(reference))
        if raw is None:
            return self.resolve_private_actor(reference)
        if not isinstance(raw, str):
            raise ProductIdentityRejected("本次家长身份已经失效。")
        try:
            scope = _FamilyScopeBinding.model_validate_json(raw)
        except (ValidationError, TypeError):
            raise ProductIdentityRejected("本次家长身份已经失效。") from None
        user, session = self._complete(scope.session_token)
        state = self._current_state(session)
        selected = state.selected_identity
        if (
            not self._family_enabled or state.family_private_ref != reference
            or selected is None or selected.context_kind != "guardian"
            or selected.school_id != scope.school_id or selected.person_id != scope.person_id
        ):
            raise ProductIdentityRejected("本次家长身份已经变化。")
        self._require_current_family_scope(scope, session, reference)
        return ProductFamilyIdentity(
            ProductFamilyActor(user.id, scope.school_id, scope.person_id, user.security_version),
            session.session_nonce_hash, reference,
        )

    def family_children(self, reference: str) -> tuple[FamilyChildChoice, ...]:
        """Enumerate the current narrow identity directory, never student records."""
        principal = self.resolve_assistant_identity(reference)
        if type(principal) is not ProductFamilyIdentity:
            raise ProductIdentityRejected("本次查询需要家长身份。")
        scope = self._family_scope_binding(reference)
        user, session = self._complete(scope.session_token)
        state = self._current_state(session)
        resolver = self._resolver_factory(user, session)
        try:
            options = resolver.list_options()
        except ContextResolutionUnavailable as exc:
            raise ProductIdentityUnavailable("当前孩子关系暂时无法核验。") from exc
        choices: list[FamilyChildChoice] = []
        for option in options:
            if option.kind != "guardian_relationship":
                continue
            try:
                actor = resolver.resolve(option.opaque_ref)
            except ContextNoLongerValid:
                # The list and resolution can straddle revocation; do not revive it.
                continue
            except ContextResolutionUnavailable as exc:
                raise ProductIdentityUnavailable("当前孩子关系暂时无法核验。") from exc
            if (
                type(actor) is not GuardianActorContext
                or actor.school_id != principal.actor.school_id
                or actor.person_id != principal.actor.person_id
                or actor.account_user_id != principal.actor.account_user_id
                or actor.authorization_version != principal.actor.authorization_version
            ):
                continue
            selected = SelectedContextIdentity.from_actor(actor, session)
            key = self._family_child_locator_key(reference, selected)
            stored = self._get(key)
            binding = None
            if isinstance(stored, str):
                try:
                    candidate = _FamilyChildBinding.model_validate_json(stored)
                    if candidate.scope_ref == reference and candidate.selected_identity == selected:
                        binding = candidate
                except ValidationError:
                    pass
            label = f"{option.school_label} · {option.display_label}"
            if binding is None:
                binding = _FamilyChildBinding(
                    scope_ref=reference, child_ref=secrets.token_urlsafe(32),
                    private_ref=secrets.token_urlsafe(32), selected_identity=selected, label=label,
                )
            elif binding.label != label:
                binding = binding.model_copy(update={"label": label})
            self._set_private(key, self._ttl(session), binding.model_dump_json())
            self._set_private(self._family_child_key(binding.child_ref), self._ttl(session),
                              binding.model_dump_json())
            self._set_private(self._private_key(binding.private_ref), self._ttl(session),
                              _PrivateBinding(session_token=scope.session_token,
                                              context_ref=reference,
                                              family_child_ref=binding.child_ref).model_dump_json())
            choices.append(FamilyChildChoice(
                child_ref=binding.child_ref, label=label,
                is_default=state.selected_identity == selected,
            ))
        if self.resolve_assistant_identity(reference) != principal:
            raise ProductIdentityRejected("当前家长身份已经变化。")
        return tuple(choices)

    def resolve_family_child(self, reference: str, child_ref: str) -> ProductIdentity:
        principal = self.resolve_assistant_identity(reference)
        if type(principal) is not ProductFamilyIdentity or type(child_ref) is not str \
                or _TOKEN.fullmatch(child_ref) is None:
            raise ProductIdentityRejected("该孩子不在当前获准范围内。")
        raw = self._get(self._family_child_key(child_ref))
        if not isinstance(raw, str):
            raise ProductIdentityRejected("该孩子引用已失效，请重新核对。")
        try:
            binding = _FamilyChildBinding.model_validate_json(raw)
            scope = self._family_scope_binding(reference)
        except (ValidationError, TypeError):
            raise ProductIdentityRejected("该孩子引用已失效，请重新核对。") from None
        selected = binding.selected_identity
        if (
            binding.scope_ref != reference or binding.child_ref != child_ref
            or selected.school_id != principal.actor.school_id
            or selected.person_id != principal.actor.person_id
            or selected.account_user_id != principal.actor.account_user_id
            or selected.authorization_version != principal.actor.authorization_version
        ):
            raise ProductIdentityRejected("该孩子不在当前获准范围内。")
        user, session = self._complete(scope.session_token)
        try:
            actor = self._resolver_factory(user, session).resolve_selected(selected)
        except ContextNoLongerValid as exc:
            raise ProductIdentityRejected("该孩子的当前查看关系已失效。") from exc
        except ContextResolutionUnavailable as exc:
            raise ProductIdentityUnavailable("当前孩子关系暂时无法核验。") from exc
        if type(actor) is not GuardianActorContext \
                or SelectedContextIdentity.from_actor(actor, session) != selected \
                or self.resolve_assistant_identity(reference) != principal:
            raise ProductIdentityRejected("当前孩子关系已经变化。")
        return ProductIdentity(
            actor, session.session_nonce_hash, binding.private_ref, binding.label
        )

    @staticmethod
    def _family_scope_key(reference: str) -> str:
        return f"school_fact_product:family_scope:{reference}"

    def _family_scope_binding(self, reference: str) -> _FamilyScopeBinding:
        raw = self._get(self._family_scope_key(reference))
        if not isinstance(raw, str):
            raise ProductIdentityRejected("本次家长身份已经失效。")
        try:
            return _FamilyScopeBinding.model_validate_json(raw)
        except ValidationError:
            raise ProductIdentityRejected("本次家长身份已经失效。") from None

    @staticmethod
    def _family_child_key(reference: str) -> str:
        return f"school_fact_product:family_child:{reference}"

    @staticmethod
    def _family_child_locator_key(reference: str, selected: SelectedContextIdentity) -> str:
        return (f"school_fact_product:family_locator:{reference}:"
                f"{selected.context_id}:{selected.context_version}")

    def resolve_request(self, token: str | None) -> ProductIdentity:
        user, identity = self._complete(token)
        assert token is not None
        state = self._state(token, user, identity)
        if state.selected_context_ref is None or state.private_ref is None:
            raise ProductIdentityRejected("请先显式选择履职身份。")
        actor = self._resolve_selection(user, identity, state)
        self._require_current_selection(token, identity, state)
        label = next(
            (choice.label for choice in state.contexts if choice.ref == state.selected_context_ref),
            None,
        )
        return ProductIdentity(
            actor, identity.session_nonce_hash, state.private_ref, label, state.selection_event
        )

    def resolve_private_actor(self, ref: str) -> ProductIdentity:
        """Share only this synchronous release; independently resolve again at its end."""
        if type(ref) is not str or _TOKEN.fullmatch(ref) is None:
            raise ProductIdentityRejected("本次助手身份已经失效。")
        batch = current_validation_batch()
        if batch is None:
            return self._resolve_private_actor(ref)
        key = (self, ref)
        evidence = batch.identity_checks.get(key)
        if evidence is None:
            identity = self._resolve_private_actor(ref)
            evidence = ProductIdentityEvidence(identity, lambda: self._resolve_private_actor(ref))
            batch.identity_checks[key] = evidence
        return evidence.identity

    def _resolve_private_actor(self, ref: str) -> ProductIdentity:
        """Complete live account/session/context resolution; never retains a connection."""
        if type(ref) is not str or _TOKEN.fullmatch(ref) is None:
            raise ProductIdentityRejected("本次助手身份已经失效。")
        raw = self._get(self._private_key(ref))
        try:
            if not isinstance(raw, str):
                raise ValueError
            payload = _PrivateBinding.model_validate_json(raw)
            if payload.family_child_ref is not None:
                return self.resolve_family_child(payload.context_ref, payload.family_child_ref)
            identity = self.resolve_request(payload.session_token)
            if identity.actor_context_ref != ref:
                raise ValueError
            return identity
        except (ValueError, TypeError) as exc:
            raise ProductIdentityRejected("本次助手身份已经失效。") from exc

    def _resolve(self, user: CurrentUser, identity: IdentitySession, ref: str) -> ActorContext:
        try:
            actor = self._resolver_factory(user, identity).resolve(ref)
            if type(actor) is not StaffActorContext and not (
                self._family_enabled and type(actor) is GuardianActorContext
            ):
                raise ProductIdentityRejected("请选择当前获准的身份或孩子关系。")
            return actor
        except ContextNoLongerValid as exc:
            raise ProductIdentityRejected("履职身份已经变化，请重新选择。") from exc
        except ContextResolutionUnavailable as exc:
            raise ProductIdentityUnavailable("履职身份暂时无法核验。") from exc

    def _resolve_selection(
        self, user: CurrentUser, identity: IdentitySession, state: _SelectedState
    ) -> ActorContext:
        if state.selected_identity is None:
            assert state.selected_context_ref is not None
            return self._resolve(user, identity, state.selected_context_ref)
        try:
            actor = self._resolver_factory(user, identity).resolve_selected(state.selected_identity)
            if type(actor) is not StaffActorContext and not (
                self._family_enabled and type(actor) is GuardianActorContext
            ):
                raise ProductIdentityRejected("请选择当前获准的身份或孩子关系。")
            if SelectedContextIdentity.from_actor(actor, identity) != state.selected_identity:
                raise ProductIdentityRejected("本次助手身份已经变化。")
            return actor
        except ContextNoLongerValid as exc:
            raise ProductIdentityRejected("履职身份已经变化，请重新选择。") from exc
        except ContextResolutionUnavailable as exc:
            raise ProductIdentityUnavailable("履职身份暂时无法核验。") from exc

    def _state(self, token: str, user: CurrentUser, identity: IdentitySession) -> _SelectedState:
        state = self._parse_state(self._get(self._state_key(identity)))
        if state is None:
            return self._new_options(token, user, identity)
        if state.selected_context_ref is not None and state.private_ref is not None:
            if state.selected_identity is None:
                # Compatibility is possible only while the original opaque option
                # still resolves. Never infer an old selection from names/order.
                try:
                    actor = self._resolve(user, identity, state.selected_context_ref)
                except ProductIdentityRejected:
                    return self._new_options(token, user, identity, previous=state)
                upgraded = state.model_copy(
                    update={
                        "selected_identity": SelectedContextIdentity.from_actor(actor, identity)
                    }
                )
                if self._replace_state(token, identity, state, upgraded):
                    return upgraded
                return self._state_after_race(identity, state)
            return state
        if state.effective_until <= datetime.now(UTC):
            return self._new_options(token, user, identity, previous=state)
        return state

    def _new_options(
        self,
        token: str,
        user: CurrentUser,
        identity: IdentitySession,
        *,
        previous: _SelectedState | None = None,
        preserve: bool = False,
    ) -> _SelectedState:
        try:
            options = tuple(
                o
                for o in self._resolver_factory(user, identity).list_options(
                    exclude_selected=previous.selected_identity if preserve and previous else None
                )
                if o.kind == "staff_assignment"
                or self._family_enabled
                and o.kind == "guardian_relationship"
            )
        except (ContextNoLongerValid, ContextResolutionUnavailable) as exc:
            raise ProductIdentityUnavailable("履职身份暂时无法枚举。") from exc
        expires = datetime.fromisoformat(identity.expires_at)
        contexts = tuple(
            IdentityChoice(
                ref=o.opaque_ref, label=f"{o.school_label} · {o.display_label}", kind=o.kind
            )
            for o in options
        )
        updates: dict[str, object] = {}
        if preserve and previous is not None and previous.selected_identity is not None:
            # The original selected UI item stays available under its original ref.
            contexts = tuple(
                c for c in previous.contexts if c.ref == previous.selected_context_ref
            ) + tuple(c for c in contexts if c.ref != previous.selected_context_ref)
            updates = {
                "selected_context_ref": previous.selected_context_ref,
                "private_ref": previous.private_ref,
                "selected_identity": previous.selected_identity,
                "selection_event": previous.selection_event,
                "selection_context_claim": previous.selection_context_claim,
                "selection_context_conversation_ref": previous.selection_context_conversation_ref,
                "selection_context_claimed_at": previous.selection_context_claimed_at,
                "family_private_ref": previous.family_private_ref,
            }
        state = _SelectedState(
            contexts=contexts,
            effective_until=min((o.effective_until for o in options), default=expires),
        ).model_copy(update=updates)
        if self._replace_state(token, identity, previous, state):
            return state
        return self._state_after_race(identity, previous)

    def _replace_state(
        self,
        token: str,
        identity: IdentitySession,
        previous: _SelectedState | None,
        state: _SelectedState,
    ) -> bool:
        raw = self._get(self._state_key(identity))
        if self._parse_state(raw) != previous:
            return False
        try:
            return (
                self._redis.eval(
                    _REPLACE_SELECTION,
                    2,
                    self._state_key(identity),
                    self._store._key(token),  # pyright: ignore[reportPrivateUsage]
                    raw if isinstance(raw, str) else "",
                    self._ttl(identity),
                    state.model_dump_json(),
                )
                == 1
            )
        except RedisError as exc:
            raise ProductIdentityUnavailable("工程身份存储暂时不可用。") from exc

    def _current_state(self, identity: IdentitySession) -> _SelectedState:
        state = self._parse_state(self._get(self._state_key(identity)))
        if state is None:
            raise ProductIdentityRejected("身份会话或履职选择已经失效。")
        return state

    def _state_after_race(
        self, identity: IdentitySession, previous: _SelectedState | None
    ) -> _SelectedState:
        current = self._current_state(identity)
        if previous is None or (
            current.selected_context_ref != previous.selected_context_ref
            or current.private_ref != previous.private_ref
            or current.selected_identity != previous.selected_identity
        ):
            raise _SelectionChanged("履职选择已经变化。")
        return current

    def _require_current_selection(
        self, token: str, identity: IdentitySession, state: _SelectedState
    ) -> None:
        # Re-read after identity IO. List refresh may change only choices; switching
        # or logout cannot resurrect the prior private actor or its receipt binding.
        try:
            if self._store.read(token) != identity:
                raise ProductIdentityRejected("身份会话已经失效。")
        except RedisError as exc:
            raise ProductIdentityUnavailable("工程身份存储暂时不可用。") from exc
        current = self._current_state(identity)
        if (
            current.selected_context_ref != state.selected_context_ref
            or current.private_ref != state.private_ref
            or current.selected_identity != state.selected_identity
        ):
            raise _SelectionChanged("履职选择已经变化。")

    def _require_current_family_scope(
        self, scope: _FamilyScopeBinding, session: IdentitySession, reference: str
    ) -> None:
        # The page may change its optional child focus during the read. A family
        # dialogue remains bound to its actual parent/school/session, while every
        # data release revalidates the requested child's own current relationship.
        _, fresh_session = self._complete(scope.session_token)
        if fresh_session != session:
            raise ProductIdentityRejected("身份会话已经失效。")
        state = self._current_state(fresh_session)
        selected = state.selected_identity
        if (
            state.family_private_ref != reference
            or selected is None or selected.context_kind != "guardian"
            or selected.school_id != scope.school_id or selected.person_id != scope.person_id
        ):
            raise ProductIdentityRejected("本次家长身份已经变化。")

    def _get(self, key: str) -> object:
        try:
            return self._redis.get(key)
        except RedisError as exc:
            raise ProductIdentityUnavailable("工程身份存储暂时不可用。") from exc

    def _set_private(self, key: str, ttl: int, value: str) -> None:
        try:
            self._redis.setex(key, ttl, value)
        except RedisError as exc:
            raise ProductIdentityUnavailable("工程身份存储暂时不可用。") from exc

    @staticmethod
    def _parse_state(raw: object) -> _SelectedState | None:
        if not isinstance(raw, str):
            return None
        try:
            state = _SelectedState.model_validate_json(raw)
            return state if state.effective_until.utcoffset() is not None else None
        except ValidationError:
            return None

    @staticmethod
    def _ttl(identity: IdentitySession) -> int:
        return max(
            1,
            min(
                SESSION_TTL_SECONDS,
                math.ceil(
                    (
                        datetime.fromisoformat(identity.expires_at) - datetime.now(UTC)
                    ).total_seconds()
                ),
            ),
        )

    @staticmethod
    def _state_key(identity: IdentitySession) -> str:
        return f"school_fact_product:selection:{identity.session_nonce_hash}"

    @staticmethod
    def _private_key(ref: str) -> str:
        return f"school_fact_product:private_actor:{ref}"


class SchoolFactProductIdentityService(SchoolFactIdentitySessionService):
    """Controlled local account entry; real session and explicit authority context stay separate."""

    def __init__(
        self,
        *,
        app_env: str,
        redis: SessionRedisClient,
        accounts: tuple[EngineeringAccount, ...],
        load_account: Callable[[EngineeringAccount], CurrentUser | None],
        resolver_factory: Callable[[CurrentUser, IdentitySession], ProductActorContextResolver],
        can_maintain: Callable[[StaffActorContext], bool],
        family_enabled: bool = False,
    ) -> None:
        if app_env not in {"development", "test"}:
            raise ValueError("engineering identity is forbidden in production")
        if (
            not accounts
            or any(
                item
                not in ENGINEERING_ACCOUNTS
                + (FAMILY_ENGINEERING_ACCOUNTS if family_enabled else ())
                for item in accounts
            )
            or len({item.ref for item in accounts}) != len(accounts)
        ):
            raise ValueError("engineering account allowlist is not approved")
        if not isinstance(redis, _ProductIdentityRedis):
            raise ValueError("product identity requires atomic Redis selection replacement")
        self._accounts = {item.ref: item for item in accounts}
        self._redis = redis
        self._store = IdentitySessionStore(redis=redis, ttl_seconds=SESSION_TTL_SECONDS)
        self._load_account = load_account
        self._resolver_factory = resolver_factory
        self._can_maintain = can_maintain
        self._family_enabled = family_enabled

    def login(self, account_ref: str) -> str:
        account = self._accounts.get(account_ref)
        if account is None:
            raise ProductIdentityRejected("请选择已初始化的合成工程账号。")
        try:
            current = self._load_account(account)
            if current is None or current.external_subject != account.external_subject:
                raise ProductIdentityRejected("该合成工程账号尚未初始化或已停用。")
            token = self._store.create(current)
            try:
                user, identity = self._complete(token)
                state = self._new_options(token, user, identity)
                if state.contexts and all(
                    option.kind == "guardian_relationship" for option in state.contexts
                ):
                    actors = [
                        self._resolve(user, identity, option.ref) for option in state.contexts
                    ]
                    if len({(actor.school_id, actor.person_id) for actor in actors}) == 1:
                        # The HTTP summary retains a compatible optional child focus;
                        # assistant target selection is independent of that focus.
                        self.select_context(token, state.contexts[0].ref)
                        self.resolve_assistant_request(token)
            except Exception:
                self._store.delete(token)
                raise
            return token
        except (RedisError, SQLAlchemyError, ContextResolutionUnavailable) as exc:
            raise ProductIdentityUnavailable("工程身份服务暂时不可用。") from exc

    def _complete(self, token: str | None) -> tuple[CurrentUser, IdentitySession]:
        if not token or _TOKEN.fullmatch(token) is None:
            raise ProductIdentityRejected("请先登录合成工程账号。")
        try:
            identity = self._store.read(token)
            if identity is None:
                raise ProductIdentityRejected("身份会话已经失效。")
            matching = [
                a
                for a in self._accounts.values()
                if a.external_subject == identity.external_subject
            ]
            if len(matching) != 1:
                raise ProductIdentityRejected("身份会话不属于获准工程账号。")
            user = self._load_account(matching[0])
            if (
                user is None
                or user.id != identity.user_id
                or user.external_subject != identity.external_subject
                or user.security_version != identity.security_version
            ):
                raise ProductIdentityRejected("账号状态已经变化，请重新登录。")
            return user, identity
        except (RedisError, SQLAlchemyError) as exc:
            raise ProductIdentityUnavailable("工程身份服务暂时不可用。") from exc


def build_school_fact_product_identity_service(
    *,
    app_env: str,
    session_factory: sessionmaker[Session],
    redis: SessionRedisClient,
    proof_signer: ContextProofSigner,
    can_maintain: Callable[[StaffActorContext], bool],
    accounts: tuple[EngineeringAccount, ...] = ENGINEERING_ACCOUNTS,
    family_enabled: bool = False,
) -> SchoolFactProductIdentityService:
    """The real composition uses read-only existing account lookup and ActorContextResolver."""

    def load_account(account: EngineeringAccount) -> CurrentUser | None:
        with session_factory() as session:
            row = session.execute(
                select(
                    User.id,
                    User.external_subject,
                    User.display_name,
                    User.security_version,
                ).where(
                    User.provider_code == account.provider_code,
                    User.provider_tenant_key == account.provider_tenant_key,
                    User.external_subject == account.external_subject,
                    User.is_active.is_(True),
                )
            ).one_or_none()
            if row is None:
                return None
            return CurrentUser(
                id=row.id,
                external_subject=row.external_subject,
                display_name=row.display_name,
                security_version=row.security_version,
            )

    return SchoolFactProductIdentityService(
        app_env=app_env,
        redis=redis,
        accounts=(
            accounts + FAMILY_ENGINEERING_ACCOUNTS
            if family_enabled and accounts == ENGINEERING_ACCOUNTS
            else accounts
        ),
        load_account=load_account,
        resolver_factory=lambda user, identity: ActorContextResolver(
            session_factory=session_factory,
            redis=redis,
            proof_signer=proof_signer,
            current_user=user,
            identity_session=identity,
        ),
        can_maintain=can_maintain,
        family_enabled=family_enabled,
    )
