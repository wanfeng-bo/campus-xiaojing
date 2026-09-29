from __future__ import annotations

from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session


def identity_tuple_advisory_lock_material(
    *,
    provider_code: str,
    provider_tenant_key: str,
    external_subject: str,
) -> str:
    """Return the fixed length-prefixed text hashed by the tuple advisory lock."""

    components = (provider_code, provider_tenant_key, external_subject)
    encoded_components = tuple(component.encode("utf-8") for component in components)
    return "v1|" + "|".join(
        f"{len(encoded)}:{component}"
        for component, encoded in zip(components, encoded_components, strict=True)
    )


class IdentityTupleValidator(Protocol):
    """Identity-owned, read-only tuple validation used by other write domains."""

    def lock_existing_user_id(
        self,
        session: Session,
        *,
        provider_code: str,
        provider_tenant_key: str,
        external_subject: str,
    ) -> int | None: ...


class SqlAlchemyIdentityTupleValidator:
    """Call the narrow Identity SQL capability in the caller's transaction."""

    def __init__(self, *, school_fact_command_wrapper: bool = False) -> None:
        self._lookup_function = (
            "app.school_fact_lock_existing_identity_user_id"
            if school_fact_command_wrapper
            else "app.lock_existing_identity_user_id"
        )

    def lock_existing_user_id(
        self,
        session: Session,
        *,
        provider_code: str,
        provider_tenant_key: str,
        external_subject: str,
    ) -> int | None:
        user_id = session.execute(
            text(
                f"SELECT {self._lookup_function}("
                ":provider_code, :provider_tenant_key, :external_subject)"
            ),
            {
                "provider_code": provider_code,
                "provider_tenant_key": provider_tenant_key,
                "external_subject": external_subject,
            },
        ).scalar_one_or_none()
        return None if user_id is None else int(user_id)
