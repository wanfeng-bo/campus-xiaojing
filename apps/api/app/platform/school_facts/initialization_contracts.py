from __future__ import annotations

from collections import Counter
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import StringConstraints, model_validator

from app.platform.contracts import FrozenContractModel
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1, NonBlankText

INITIALIZATION_HEAD = "20260905_22"
INITIALIZATION_AUTHORITY = "deployment:campus_ai_migration"


class SchoolFactInitializationRejected(RuntimeError):
    """Controlled initialization failed without exposing private inputs."""


class SchoolFactInitializationRequest(FrozenContractModel):
    school_id: UUID
    anchor_id: UUID
    expected_system_identifier: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,32}$")]
    request_id: UUID
    approval_reference: NonBlankText
    operator_reference: NonBlankText
    change_set: CanonicalFactChangeSetV1

    @model_validator(mode="after")
    def require_minimum_identity_manifest(self) -> Self:
        changes = self.change_set.changes
        counts = Counter(change.fact_kind for change in changes)
        allowed = {"school", "person", "organization", "login_mapping", "assignment"}
        if (
            set(counts) - allowed
            or len(changes) > 32
            or any(counts[kind] != 1 for kind in ("person", "login_mapping", "assignment"))
            or counts["organization"] < 1
            or counts["school"] > 1
        ):
            raise ValueError("Initialization is limited to the first maintainer identity.")
        for change in changes:
            if change.operation != "upsert" or change.expected_version != (
                1 if change.fact_kind == "school" else 0
            ):
                raise ValueError("Initialization cannot alter existing people or relationships.")
        person = next(change for change in changes if change.fact_kind == "person")
        if (
            person.content is None
            or person.content.person_kind != "staff"
            or person.content.lifecycle_status != "active"
        ):
            raise ValueError("Initialization requires one active staff person.")
        organizations = {
            change.source_key for change in changes if change.fact_kind == "organization"
        }
        for change in changes:
            content = change.content
            if content is None:
                raise ValueError("Initialization requires explicit content.")
            if change.fact_kind in {"login_mapping", "assignment"}:
                if content.person_source_key != person.source_key or content.status != "confirmed":
                    raise ValueError("Initial relationships must confirm the same first person.")
            if (
                change.fact_kind == "assignment"
                and content.organization_source_key not in organizations
            ):
                raise ValueError("Initial assignment must refer to the approved organization.")
            if change.fact_kind == "organization" and (
                content.status != "active"
                or (
                    content.parent_source_key is not None
                    and content.parent_source_key not in organizations
                )
            ):
                raise ValueError("Initial organization dependencies must be in the manifest.")
        return self

    def manifest_sha256(self) -> str:
        return sha256(self.model_dump_json().encode("utf-8")).hexdigest()


def require_initialization_authority(*, session_user: str, alembic_heads: tuple[str, ...]) -> None:
    if session_user != "campus_ai_migration" or alembic_heads != (INITIALIZATION_HEAD,):
        raise SchoolFactInitializationRejected("Initialization authority or schema rejected.")


def require_initialization_replay(
    *,
    state: str,
    original_sha256: str,
    request_sha256: str,
    action: Literal["prepare", "confirm", "close"],
) -> None:
    if original_sha256 != request_sha256 or (state == "closed" and action != "close"):
        raise SchoolFactInitializationRejected(
            "Initialization is closed or the approved manifest changed."
        )
