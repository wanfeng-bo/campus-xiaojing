"""Minimal F6 component assembly; configuration and queries do not grant authority."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.composition.school_fact_residence_authority import create_residence_authority
from app.composition.school_fact_residence_query import (
    FamilyResidenceQueryService,
    ResidenceQueryService,
)
from app.composition.school_fact_residence_reader import (
    ResidenceRepository,
    SqlFamilyResidenceRepository,
    SqlResidenceProjection,
)
from app.platform.school_facts.f6_policy import F6_FACT_KINDS


@dataclass
class ResidenceComponents:
    authority: Any
    repository: ResidenceRepository
    query: ResidenceQueryService
    family_query: FamilyResidenceQueryService

    def require_maintenance(self, actor: Any, change_set: Any) -> Any:
        return self.authority.require_change_set(actor, change_set)

    def require_receipt(self, actor: Any) -> Any:
        # Eligibility only; caller must additionally authorize every actual
        # receipt target/field. This never exposes receipt content by itself.
        for kind in sorted(F6_FACT_KINDS):
            try:
                self.authority.require_scope(
                    actor, fact_kind=kind, action="read", checked_at=datetime.now(UTC)
                )
                return
            except PermissionError:
                continue
        # An explicitly authorized creator may recover their original audit
        # receipt without acquiring fact read access. The Writer checks the
        # exact original creation and returns metadata only in that case.
        for kind in sorted(F6_FACT_KINDS):
            try:
                permit = self.authority.require_scope(
                    actor, fact_kind=kind, action="maintain", checked_at=datetime.now(UTC)
                )
                if permit.scope.creation_keys:
                    return
            except PermissionError:
                continue
        raise PermissionError("当前没有可核对该寄宿回执的权限。")


def create_residence_components(
    *,
    sessions: Any,
    configuration: Any,
    reference_key: Any,
    current_actor: Any,
    resolve_related: Any = None,
    related_reference_for: Any = None,
    time_context: Any = None,
) -> Any:
    authority = create_residence_authority(sessions=sessions, configuration=configuration)
    repository = ResidenceRepository(
        authority=authority,
        projection=SqlResidenceProjection(sessions),
        reference_key=reference_key,
        resolve_related=resolve_related,
        related_reference_for=related_reference_for,
    )
    return ResidenceComponents(
        authority,
        repository,
        ResidenceQueryService(
            repository=repository,
            current_actor=current_actor,
            reference_key=reference_key,
            time_context=time_context,
        ),
        FamilyResidenceQueryService(
            repository=SqlFamilyResidenceRepository(sessions),
            current_actor=current_actor,
            reference_key=reference_key,
            time_context=time_context,
        ),
    )
