"""Request-local binding to an existing authenticated identity; never issues an actor."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.composition.school_fact_assistant_gateway import ProductActor
from app.platform.contracts import ActorContext, GuardianActorContext, StaffActorContext

_PRIVATE_IDENTITY: ContextVar[str | None] = ContextVar("f2_private_identity", default=None)


def resolve_bound_identity(actor: Any, resolve: Any) -> Any:
    """Retrieve only the caller's already selected live identity, without fallback."""
    reference = _PRIVATE_IDENTITY.get()
    if reference is None or type(actor) not in {StaffActorContext, GuardianActorContext}:
        raise PermissionError("未绑定当前身份。")
    identity = resolve(reference)
    if type(identity.actor) is not type(actor) or identity.actor != actor:
        raise PermissionError("当前身份已变化。")
    return identity


@contextmanager
def bind_directory_identity(reference: str) -> Iterator[None]:
    token = _PRIVATE_IDENTITY.set(reference)
    try:
        yield
    finally:
        _PRIVATE_IDENTITY.reset(token)


def current_directory_actor_matches(
    actor: StaffActorContext, resolve: Callable[[str], ProductActor]
) -> bool:
    reference = _PRIVATE_IDENTITY.get()
    if reference is None or type(actor) is not StaffActorContext:
        return False
    try:
        current = resolve(reference).actor
    except Exception:
        return False
    return type(current) is StaffActorContext and all(
        getattr(current, key) == getattr(actor, key)
        for key in (
            "account_user_id",
            "school_id",
            "person_id",
            "assignment_id",
            "assignment_version",
            "authorization_version",
        )
    )


def current_bound_actor_matches(actor: ActorContext, resolve: Callable[[str], object]) -> bool:
    """Compare the current trusted identity at use; never derives or grants authority."""
    reference = _PRIVATE_IDENTITY.get()
    if reference is None or type(actor) not in {StaffActorContext, GuardianActorContext}:
        return False
    try:
        current = getattr(resolve(reference), "actor", None)
        return type(current) is type(actor) and current == actor
    except Exception:
        return False
