"""Fixed runtime error categories for the PydanticAI boundary.

No exception, traceback, message or provider object is retained by these helpers.
Only exact, existing runtime classes are admitted; unlisted subclasses fall back
rather than inheriting an HTTP meaning implicitly.
"""

from app.platform.assistant.runtime import (
    AssistantRuntimeError,
    ConversationAccessError,
    ConversationBusyError,
    ConversationClosedError,
    InvalidRuntimeInputError,
    RuntimeInvariantError,
    StaleRunError,
)

_RUNTIME_MESSAGES: dict[type[AssistantRuntimeError], str] = {
    AssistantRuntimeError: "candidate run no longer usable",
    InvalidRuntimeInputError: "candidate runtime input is invalid",
    ConversationAccessError: "candidate conversation is unavailable",
    ConversationBusyError: "candidate conversation is busy",
    ConversationClosedError: "candidate conversation is closed",
    StaleRunError: "candidate run is no longer current",
    RuntimeInvariantError: "candidate runtime invariant unavailable",
}


def runtime_error_category(error: BaseException) -> type[AssistantRuntimeError]:
    """Return a whitelisted class only, never the original exception object."""
    for category in _RUNTIME_MESSAGES:
        if type(error) is category:
            return category
    return AssistantRuntimeError


def rebuild_runtime_error(
    error: BaseException | type[AssistantRuntimeError],
) -> AssistantRuntimeError:
    """Make a fresh safe error from an exception or a previously saved category.

    This returns rather than raises, so callers can raise it outside their except
    handlers and avoid Python attaching the original exception as its context.
    """
    if isinstance(error, type):
        category = next(
            (kind for kind in _RUNTIME_MESSAGES if error is kind), AssistantRuntimeError
        )
    else:
        category = runtime_error_category(error)
    return category(_RUNTIME_MESSAGES[category])
