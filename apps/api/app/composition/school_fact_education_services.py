"""F4 composition of the existing identity, Readers, PydanticAI tools and sole Writer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
from app.composition.school_fact_directory_services import create_directory_read_components
from app.composition.school_fact_directory_settings import DirectoryConfiguration
from app.composition.school_fact_education_authority import create_education_authority
from app.composition.school_fact_education_dependencies import EducationDependencyProductAdapter
from app.composition.school_fact_education_external import (
    EducationExternalReader,
    SqlEducationExternalBoundaries,
)
from app.composition.school_fact_education_maintenance import EducationMaintenanceCoordinator
from app.composition.school_fact_education_product import EducationProductAdapter
from app.composition.school_fact_education_quality import (
    EducationDependencyReferenceAdapter,
    EducationQualityBridge,
    SqlEducationDependencyProjection,
)
from app.composition.school_fact_education_query import EducationQueryService
from app.composition.school_fact_education_reader import EducationRepository, SqlEducationProjection
from app.composition.school_fact_education_views import EducationMaintenanceViews
from app.composition.school_fact_family_services import create_family_staff_components
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.composition.school_fact_read_connection import F2ConnectionReadExecutor
from app.platform.identity.execution_context import compose_command_context
from app.platform.school_facts.f5_policy import F5_OPERATIONS


@dataclass
class EducationComponents:
    authority: Any
    repository: EducationRepository
    query: EducationQueryService
    views: EducationMaintenanceViews
    maintenance: EducationMaintenanceCoordinator
    family: Any
    dependency_directory: Any
    quality: EducationQualityBridge
    dependency_references: EducationDependencyReferenceAdapter
    dependencies: EducationDependencyProductAdapter

    def require_maintenance(self, actor: Any, change_set: Any) -> None:
        from app.domains.students.education_contracts import EDUCATION_FACT_KINDS

        education = tuple(c for c in change_set.changes if c.fact_kind in EDUCATION_FACT_KINDS)
        dependencies = tuple(
            c for c in change_set.changes if c.fact_kind in {"fact_quality", "policy_scope"}
        )
        retained = tuple(
            c
            for c in change_set.changes
            if c.fact_kind not in EDUCATION_FACT_KINDS | {"fact_quality", "policy_scope"}
        )
        if dependencies:
            self.quality.require_change_set(
                actor, change_set.model_copy(update={"changes": dependencies})
            ).revalidate()
        if education:
            self.authority.require_change_set(
                actor,
                change_set.model_copy(update={"changes": education}),
                checked_at=datetime.now(UTC),
            )
        if retained:
            if any(c.fact_kind != "student_school_relationship" for c in retained):
                raise PermissionError("教育变更不能修改无关领域资料。")
            self.family.require_maintenance(
                actor, change_set.model_copy(update={"changes": retained})
            )

    def require_receipt(self, actor: Any) -> None:
        proof = self.authority.own_result(
            compose_command_context(actor, uuid4(), "education-receipt")
        )
        proof.revalidate()


def create_education_components(
    *,
    sessions: Any,
    configuration: Any,
    reference_key: bytes,
    directory: Any,
    directory_adapter: Any,
    family: Any,
    reader_settings: Any,
    legacy_scope_id: Any,
) -> EducationComponents:
    # Missing dependency configuration means an empty binding set, never legacy fallback.
    dependency_directory = create_directory_read_components(
        sessions=sessions,
        settings=reader_settings,
        configuration=configuration.dependency_directory
        or DirectoryConfiguration(
            approval_reference=configuration.approval_reference,
            scope_bindings=(),
        ),
        legacy_scope_id=legacy_scope_id,
        reference_key=reference_key,
    )
    dependency_family = create_family_staff_components(
        sessions=sessions,
        configuration=configuration.dependency_family
        or FamilyConfiguration(
            approval_reference=configuration.approval_reference,
            scope_bindings=(),
        ),
        reference_key=reference_key,
        directory=dependency_directory,
    )
    directory = dependency_directory
    family = dependency_family
    directory_adapter = DirectoryProductAdapter(directory.query, evidence=directory.evidence)
    authority = create_education_authority(sessions=sessions, configuration=configuration)
    repository = EducationRepository(
        authority=authority,
        reference_key=reference_key,
        projection=SqlEducationProjection(
            F2ConnectionReadExecutor(sessions, authority.connection_authority),
            current_sessions=authority.connection_authority.current_sessions,
        ),
    )
    quality = EducationQualityBridge(
        f2_guard=directory.guard,
        education_authority=authority,
        projection=SqlEducationDependencyProjection(
            repository.projection,
            f5_enabled=any(b.operation_code in F5_OPERATIONS for b in configuration.scope_bindings),
        ),
    )
    dependency_references = EducationDependencyReferenceAdapter(
        bridge=quality, education_repository=repository, directory_repository=directory.query
    )
    external = EducationExternalReader(
        directory=directory.query,
        evidence=directory.evidence,
        family=family,
        quality_bridge=quality,
        boundary_reader=SqlEducationExternalBoundaries(
            sessions=sessions, guard=directory.guard, family=family
        )
        if any(b.operation_code in F5_OPERATIONS for b in configuration.scope_bindings)
        else None,
    )
    adapter = EducationProductAdapter(repository=repository, external=external)
    dependencies = EducationDependencyProductAdapter(
        bridge=quality,
        references=dependency_references,
        directory_adapter=directory_adapter,
        repository=repository,
    )
    return EducationComponents(
        authority,
        repository,
        EducationQueryService(repository, external_reader=external),
        EducationMaintenanceViews(
            adapter, directory=directory_adapter, family=family, dependencies=dependencies
        ),
        EducationMaintenanceCoordinator(adapter, family=family, dependencies=dependencies),
        family,
        dependency_directory,
        quality,
        dependency_references,
        dependencies,
    )
