# pyright: strict
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.platform.authorization.thin_contracts import (
    ModuleAuthorizationRequirement,
    ThinAuthorizationRequest,
)
from app.platform.contracts import StaffActorContext
from app.platform.governance.contracts import ScopeResolutionRequest, ScopeResolutionSnapshot

OFFICIAL_CONTACT_MODULE = "school_facts.official_contacts"
OFFICIAL_CONTACT_READ = "school_facts.official_contacts.read"
OFFICIAL_CONTACT_MAINTAIN = "school_facts.official_contacts.maintain"
OFFICIAL_CONTACT_READER_DUTY = "official_contact_reader"
OFFICIAL_CONTACT_MAINTAINER_DUTY = "official_contact_maintainer"
OFFICIAL_CONTACT_CAMPUSES = "school_facts.official_contact_campuses"
OFFICIAL_CONTACT_MAINTENANCE_TARGET = "school_facts.official_contact_maintenance_target"
OFFICIAL_CONTACTS = "school_facts.official_contacts"
SCHOOL_SCOPE_OWNER = "school_facts.school"
SCHOOL_SCOPE_SCHEMA = "single_school"


class SchoolFactAuthorizationUnavailable(RuntimeError):
    """An authorization authority is unavailable; this is not an absence of permission."""


class OfficialContactPolicy:
    """Own only the two F1 fact actions; a title or technical role grants nothing."""

    module_name = OFFICIAL_CONTACT_MODULE
    version = 1

    def requirement_for(
        self, request: ThinAuthorizationRequest
    ) -> ModuleAuthorizationRequirement | None:
        if (
            type(request.actor) is not StaffActorContext
            or request.module_name != self.module_name
            or request.resource_id != UUID(str(request.actor.school_id))
            or request.resource_revision != request.actor.authorization_version
            or request.resource_type
            not in {
                OFFICIAL_CONTACT_CAMPUSES,
                OFFICIAL_CONTACTS,
                OFFICIAL_CONTACT_MAINTENANCE_TARGET,
            }
        ):
            return None
        if (
            request.action_code == OFFICIAL_CONTACT_READ
            and request.resource_type != OFFICIAL_CONTACT_MAINTENANCE_TARGET
        ):
            projection = (
                "official_contact_campuses"
                if request.resource_type == OFFICIAL_CONTACT_CAMPUSES
                else "published_official_contacts"
            )
            return ModuleAuthorizationRequirement(OFFICIAL_CONTACT_READER_DUTY, projection, 1)
        if request.action_code == OFFICIAL_CONTACT_MAINTAIN and request.resource_type in {
            OFFICIAL_CONTACTS,
            OFFICIAL_CONTACT_MAINTENANCE_TARGET,
        }:
            return ModuleAuthorizationRequirement(
                OFFICIAL_CONTACT_MAINTAINER_DUTY,
                "official_contact_maintenance_target"
                if request.resource_type == OFFICIAL_CONTACT_MAINTENANCE_TARGET
                else "official_contact_maintenance",
                1,
            )
        return None


def school_scope_document_hash(school_id: UUID) -> str:
    payload = json.dumps(
        {"school_id": str(school_id)}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SingleSchoolScopeResolver:
    """Resolve one explicit school reference, without organizational descendants."""

    def resolve(self, request: ScopeResolutionRequest) -> ScopeResolutionSnapshot | None:
        if (
            request.schema_owner != SCHOOL_SCOPE_OWNER
            or request.schema_name != SCHOOL_SCOPE_SCHEMA
            or request.schema_version != 1
            or request.reference_document != {"school_id": str(request.school_id)}
            or request.reference_sha256 != school_scope_document_hash(request.school_id)
            or request.as_of.utcoffset() is None
        ):
            return None
        return ScopeResolutionSnapshot(
            school_id=request.school_id,
            owner=SCHOOL_SCOPE_OWNER,
            reference=f"school:{request.school_id}",
            version=1,
            is_effective=True,
            is_ambiguous=False,
            valid_until=None,
        )


@dataclass(frozen=True, slots=True)
class OfficialContactCampus:
    campus_ref: str
    display_name: str
    display_path: str
    version: int


@dataclass(frozen=True, slots=True)
class OfficialContactFact:
    fact_ref: str
    campus_ref: str
    campus_name: str
    contact_kind: str
    contact_value: str
    service_hours: str | None
    valid_from: datetime
    valid_to: datetime | None
    version: int
    source_kind: str
    source_description: str
    publication_ref: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class OfficialContactQuerySelection:
    subject: Literal["official_contacts", "campuses"] = "official_contacts"
    scope: Literal["selected", "all_authorized"] = "selected"
    campus_name: str | None = None
    campus_ref: str | None = None


@dataclass(frozen=True, slots=True)
class OfficialContactQueryResult:
    status: Literal[
        "known",
        "not_recorded",
        "no_current_records",
        "selection_required",
        "unmatched",
        "ambiguous",
        "forbidden",
        "source_unavailable",
    ]
    queried_at: datetime
    coverage: Literal["unknown", "partial"]
    query_scope: str
    campuses: tuple[OfficialContactCampus, ...] = ()
    contacts: tuple[OfficialContactFact, ...] = ()
    has_more: bool = False
    query: OfficialContactQuerySelection = OfficialContactQuerySelection()
    matched_count: int | None = None
