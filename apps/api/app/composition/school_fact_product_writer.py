# pyright: strict
"""Independent product Writer factory; the legacy Writer cannot opt into F1 without Policy."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, StrictBool
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
from app.composition.school_fact_directory_services import create_directory_read_components
from app.composition.school_fact_directory_settings import DirectoryConfiguration
from app.composition.school_fact_education_execution import (
    EducationDependencyFinalGuard,
    EducationFinalGuard,
    EducationOwnResultAuthorizer,
    EducationWriterDispatch,
)
from app.composition.school_fact_education_services import create_education_components
from app.composition.school_fact_education_settings import EducationConfiguration
from app.composition.school_fact_family_authority import (
    F3OwnResultAuthorizer,
    create_family_authority_components,
)
from app.composition.school_fact_family_correction_writer import (
    F2_CORRECTION_FIELDS,
    create_family_correction_kernel,
)
from app.composition.school_fact_family_execution import (
    FamilyFinalAuthorizationGuard,
    FamilyWriterDispatch,
    PartitionedOwnResultAuthorizer,
)
from app.composition.school_fact_family_quality import (
    FamilyQualityAuthorizer,
    FamilyQualityChangeGuard,
    FamilyQualityCurrentContent,
    FamilyQualityExistingContent,
    FamilyQualityFinalGuard,
    FamilyQualityReader,
    FamilyQualityWriterDispatch,
    SqlFamilyQualityProjection,
)
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.composition.school_fact_package_authorization import (
    PackageExistingQualityKind,
    PackageFactPartition,
    PackageReferenceAuthorization,
    PackageWriterAuthorization,
    package_quality_read_proofs,
)
from app.composition.school_fact_product_services import create_product_thin_service
from app.composition.school_fact_query_repository import OfficialContactFinalAuthorizationGuard
from app.composition.school_fact_read_connection import F2ConnectionReadExecutor
from app.composition.school_fact_reader import (
    SchoolFactReaderSettings,
    build_school_fact_reader_engine,
    verify_school_fact_reader_database,
)
from app.composition.school_fact_residence_authority import create_residence_authority
from app.composition.school_fact_residence_execution import (
    ResidenceFinalGuard,
    ResidenceOwnResultAuthorizer,
    ResidenceQualityFinalGuard,
    ResidenceWriterDispatch,
)
from app.composition.school_fact_residence_quality import (
    ResidenceQualityBridge,
    ResidenceQualityOwnerAuthorizer,
)
from app.composition.school_fact_residence_reader import SqlResidenceProjection
from app.composition.school_fact_residence_settings import ResidenceConfiguration
from app.composition.school_fact_writer_executor import (
    SqlAlchemySchoolFactWriterExecutorKernel,
    create_school_fact_writer_executor_app,
)
from app.composition.school_fact_writer_settings import (
    SchoolFactWriterSettings,
    build_school_fact_writer_engine,
    verify_school_fact_writer_database,
)
from app.platform.contracts import CommandExecutionContext
from app.platform.identity.executor_context_codec import CommandContextTransportCodec
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from app.platform.school_facts.f2_policy import F2FactScope
from app.platform.school_facts.f5_policy import F5_FACT_KINDS, F5_OPERATIONS
from app.platform.school_facts.family_leadership_policy import FamilyLeadershipPolicy


class _InitOnlySettings:
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


class ExplicitReaderSettings(_InitOnlySettings, SchoolFactReaderSettings):
    pass


class ExplicitWriterSettings(_InitOnlySettings, SchoolFactWriterSettings):
    pass


class ProductWriterConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    reader: ExplicitReaderSettings
    writer: ExplicitWriterSettings
    scope_id: UUID
    directory: DirectoryConfiguration | None = None
    family: FamilyConfiguration | None = None
    education: EducationConfiguration | None = None
    family_leadership: FamilyLeadershipPolicy | None = None
    f5_features_enabled: StrictBool = True
    residence: ResidenceConfiguration | None = None
    f6_features_enabled: StrictBool = True
    f7_packages_enabled: StrictBool = True


class DirectoryCorrectionAuthority:
    """Bind the F2 correction purpose to its existing exact maintenance permit."""

    def __init__(self, guard):
        self.guard = guard

    def require_action(self, *, actor, fact_kind, source_key, field_name, action, checked_at):
        if action != "maintain" or (fact_kind, field_name) not in F2_CORRECTION_FIELDS:
            raise PermissionError("当前维护职责不支持这项核实。")
        permit = self.guard.require_current(actor, fact_kind, purpose="maintain")
        scope = F2FactScope.from_reference(permit.scope_plan.reference)
        if (
            scope.school_id != actor.school_id
            or scope.fact_kind != fact_kind
            or source_key not in scope.object_keys
        ):
            raise PermissionError("对象不在当前维护范围内。")
        self.guard.revalidate(permit, actor)
        return permit


def require_f5_writer_schema(configuration):
    education = configuration.education
    has_f5 = education is not None and (
        any(b.operation_code in F5_OPERATIONS for b in getattr(education, "scope_bindings", ()))
        or bool(set(getattr(education, "creation_catalog", {})) & F5_FACT_KINDS)
    )
    enabled = getattr(configuration, "f5_features_enabled", True)
    allowed = (
        {
            "school-facts-f5-v41",
            "school-facts-f6-v44",
            "school-facts-f6-v45",
            "school-facts-f7-v47",
            "school-facts-f7-v48",
            "school-facts-f7-v49",
            "school-facts-f7-v50",
            "school-facts-f7-v51",
            "school-facts-f7-v52",
            "school-facts-f7-v53",
            "school-facts-f7-v54",
            "school-facts-f7-v55",
            "school-facts-f7-v56", "school-facts-live-v57",
        }
        if enabled
        else {
            "school-facts-f5-v35",
            "school-facts-f5-v36",
            "school-facts-f5-v37",
            "school-facts-f5-v38",
            "school-facts-f5-v39",
            "school-facts-f5-v40",
            "school-facts-f5-v41",
            "school-facts-f6-v44",
            "school-facts-f6-v45",
            "school-facts-f7-v47",
            "school-facts-f7-v48",
            "school-facts-f7-v49",
            "school-facts-f7-v50",
            "school-facts-f7-v51",
            "school-facts-f7-v52",
            "school-facts-f7-v53",
            "school-facts-f7-v54",
            "school-facts-f7-v55",
            "school-facts-f7-v56", "school-facts-live-v57",
        }
    )
    if (
        has_f5
        or getattr(configuration, "family_leadership", None) is not None
        or configuration.reader.schema_contract
        in {
            "school-facts-f5-v35",
            "school-facts-f5-v36",
            "school-facts-f5-v37",
            "school-facts-f5-v38",
            "school-facts-f5-v39",
            "school-facts-f5-v40",
            "school-facts-f5-v41",
        }
        or not enabled
    ) and (
        configuration.reader.schema_contract not in allowed
        or configuration.writer.schema_contract != configuration.reader.schema_contract
    ):
        raise ValueError(
            "Teaching requires schema41; disabled compatibility requires schema35-through-41"
        )
    is_f6 = configuration.reader.schema_contract in {
        "school-facts-f6-v44",
        "school-facts-f6-v45",
        "school-facts-f7-v47",
        "school-facts-f7-v48",
        "school-facts-f7-v49",
        "school-facts-f7-v50",
        "school-facts-f7-v51",
        "school-facts-f7-v52",
        "school-facts-f7-v53",
        "school-facts-f7-v54",
        "school-facts-f7-v55",
        "school-facts-f7-v56", "school-facts-live-v57",
    }
    if (getattr(configuration, "residence", None) is not None) != is_f6:
        raise ValueError("Residence authority requires matching F6 schema and configuration.")
    if is_f6 and (
        configuration.family is None
        or configuration.education is None
        or configuration.directory is None
        or configuration.residence.dependency_education is None
    ):
        raise ValueError("F6 retains all existing family and education boundaries.")


def create_school_fact_product_writer_app() -> FastAPI:
    path = Path(os.environ["SCHOOL_FACT_F1_WRITER_CONFIG_FILE"])
    configuration = ProductWriterConfiguration.model_validate_json(path.read_text(encoding="utf-8"))
    reader_settings, writer_settings = configuration.reader, configuration.writer
    require_f5_writer_schema(configuration)
    expected_contracts = (
        (
            "school-facts-f4-v29",
            "school-facts-f4-v30",
            "school-facts-f4-v31",
            "school-facts-f5-v32",
            "school-facts-f5-v33",
            "school-facts-f5-v34",
            "school-facts-f5-v35",
            "school-facts-f5-v36",
            "school-facts-f5-v37",
            "school-facts-f5-v38",
            "school-facts-f5-v39",
            "school-facts-f5-v40",
            "school-facts-f5-v41",
            "school-facts-f6-v44",
            "school-facts-f6-v45",
            "school-facts-f7-v47",
            "school-facts-f7-v48",
            "school-facts-f7-v49",
            "school-facts-f7-v50",
            "school-facts-f7-v51",
            "school-facts-f7-v52",
            "school-facts-f7-v53",
            "school-facts-f7-v54",
            "school-facts-f7-v55",
            "school-facts-f7-v56", "school-facts-live-v57",
        )
        if configuration.education is not None
        else ("school-facts-f3-v27",)
        if configuration.family is not None
        else ("school-facts-f2-v25",)
        if configuration.directory is not None
        else ("school-facts-f1-v22",)
    )
    if (
        reader_settings.schema_contract not in expected_contracts
        or writer_settings.schema_contract != reader_settings.schema_contract
    ):
        raise ValueError("Product Writer requires the exact configured schema contract.")
    if configuration.family is not None and configuration.directory is None:
        raise ValueError("F3 retains the existing directory boundary.")
    if configuration.education is not None and configuration.family is None:
        raise ValueError("F4 retains the family authority and receipt boundary.")
    if configuration.family_leadership is not None and (
        reader_settings.schema_contract
        not in {
            "school-facts-f5-v35",
            "school-facts-f5-v36",
            "school-facts-f5-v37",
            "school-facts-f5-v38",
            "school-facts-f5-v39",
            "school-facts-f5-v40",
            "school-facts-f5-v41",
            "school-facts-f6-v44",
            "school-facts-f6-v45",
            "school-facts-f7-v47",
            "school-facts-f7-v48",
            "school-facts-f7-v49",
            "school-facts-f7-v50",
            "school-facts-f7-v51",
            "school-facts-f7-v52",
            "school-facts-f7-v53",
            "school-facts-f7-v54",
            "school-facts-f7-v55",
            "school-facts-f7-v56", "school-facts-live-v57",
        }
        or configuration.family_leadership.school_id != reader_settings.expected_school_id
    ):
        raise ValueError("Leadership policy requires the same completed F5 schema and school.")
    if (
        reader_settings.expected_school_id,
        reader_settings.expected_anchor_id,
        reader_settings.expected_system_identifier,
    ) != (
        writer_settings.expected_school_id,
        writer_settings.expected_anchor_id,
        writer_settings.expected_system_identifier,
    ):
        raise ValueError("F1 Writer/Reader anchors must match.")
    if reader_settings.database_password == writer_settings.database_password:
        raise ValueError("F1 database roles require independent credentials.")
    reader_engine = build_school_fact_reader_engine(reader_settings)
    writer_engine = None
    try:
        verify_school_fact_reader_database(reader_engine, settings=reader_settings)
        thin = create_product_thin_service(sessionmaker(bind=reader_engine, expire_on_commit=False))
        guard = OfficialContactFinalAuthorizationGuard(
            thin_service=thin,
            scope_id=configuration.scope_id,
        )
        final_guard: Callable[
            [Session, CommandExecutionContext, CanonicalFactChangeSetV1], None
        ] = guard
        own_result_authorizer = None
        own_terminal_authorizer = None
        directory = None
        if configuration.directory is not None:
            directory = create_directory_read_components(
                sessions=sessionmaker(bind=reader_engine, expire_on_commit=False, autoflush=False),
                settings=reader_settings,
                configuration=configuration.directory,
                legacy_scope_id=configuration.scope_id,
                reference_key=reader_settings.cursor_hmac_key.get_secret_value().encode(),
            )

            # Input version is a data contract, never a permission fallback.
            final_guard = directory.guard
            own_result_authorizer = directory.own_result_authorizer
            own_terminal_authorizer = directory.own_terminal_authorizer
        family_actor_validator = None
        family = None
        if configuration.family is not None:
            if own_result_authorizer is None:
                raise ValueError("F3 requires the retained own receipt boundary.")
            family = create_family_authority_components(
                session_factory=sessionmaker(
                    bind=reader_engine, expire_on_commit=False, autoflush=False
                ),
                configuration=configuration.family,
            )
            final_guard = FamilyWriterDispatch(
                family=FamilyFinalAuthorizationGuard(
                    family.change_set_guard, enabled=configuration.family.enabled
                ),
                directory=final_guard,
                configuration=configuration.family,
            )
            own_result_authorizer = PartitionedOwnResultAuthorizer(
                dispatch=final_guard,
                directory=own_result_authorizer,
                family=F3OwnResultAuthorizer(
                    thin=family.thin,
                    configuration=configuration.family,
                    clock=lambda: datetime.now(UTC),
                ),
            )
            family_actor_validator = family.actor_validator
        education_authority = None
        if configuration.education is not None:
            if own_result_authorizer is None or directory is None:
                raise ValueError("F4 requires the retained own receipt boundary.")
            education_components = create_education_components(
                sessions=sessionmaker(bind=reader_engine, expire_on_commit=False, autoflush=False),
                reader_settings=reader_settings,
                legacy_scope_id=configuration.scope_id,
                configuration=configuration.education,
                reference_key=reader_settings.cursor_hmac_key.get_secret_value().encode(),
                directory=directory,
                directory_adapter=DirectoryProductAdapter(
                    directory.query, evidence=directory.evidence
                ),
                family=family,
            )
            education_authority = education_components.authority
            dependency_directory = education_components.dependency_directory
            dependency_family = education_components.family
            dependency_dispatch = FamilyWriterDispatch(
                family=FamilyFinalAuthorizationGuard(dependency_family.authority.change_set_guard),
                directory=dependency_directory.guard,
                configuration=dependency_family.configuration,
            )
            final_guard = EducationWriterDispatch(
                education=EducationFinalGuard(education_authority),
                retained=final_guard,
                dependency=EducationDependencyFinalGuard(
                    quality=education_components.quality, retained=dependency_dispatch
                ),
            )
            own_result_authorizer = EducationOwnResultAuthorizer(
                education=education_authority.own_result,
                original_authority=education_authority,
                retained=own_result_authorizer,
                dependency=PartitionedOwnResultAuthorizer(
                    dispatch=dependency_dispatch,
                    directory=dependency_directory.own_result_authorizer,
                    family=F3OwnResultAuthorizer(
                        thin=dependency_family.authority.thin,
                        configuration=dependency_family.configuration,
                        clock=lambda: datetime.now(UTC),
                    ),
                ),
            )
        residence_authority = None
        if configuration.residence is not None:
            if own_result_authorizer is None:
                raise ValueError("F6 requires retained own receipt controls.")
            if configuration.residence.dependency_education is None:
                raise ValueError("F6 requires its independent exact dependency bindings.")
            residence_dependencies = create_education_components(
                sessions=sessionmaker(bind=reader_engine, expire_on_commit=False, autoflush=False),
                reader_settings=reader_settings,
                legacy_scope_id=configuration.scope_id,
                configuration=configuration.residence.dependency_education,
                reference_key=reader_settings.cursor_hmac_key.get_secret_value().encode(),
                directory=directory,
                directory_adapter=DirectoryProductAdapter(
                    directory.query, evidence=directory.evidence
                ),
                family=family,
            )
            residence_directory = residence_dependencies.dependency_directory
            residence_family = residence_dependencies.family
            residence_dependency_dispatch = FamilyWriterDispatch(
                family=FamilyFinalAuthorizationGuard(residence_family.authority.change_set_guard),
                directory=residence_directory.guard,
                configuration=residence_family.configuration,
            )
            residence_dependency_guard = EducationWriterDispatch(
                education=EducationFinalGuard(residence_dependencies.authority),
                retained=residence_dependency_dispatch,
                dependency=residence_dependency_dispatch,
            )

            def require_residence_dependencies(session, context, change_set):
                if not configuration.f6_features_enabled:
                    raise SchoolFactChangeRejected("寄宿关联维护目前未启用。")
                return residence_dependency_guard(session, context, change_set)

            residence_dependency_receipts = EducationOwnResultAuthorizer(
                education=residence_dependencies.authority.own_result,
                original_authority=residence_dependencies.authority,
                retained=PartitionedOwnResultAuthorizer(
                    dispatch=residence_dependency_dispatch,
                    directory=residence_directory.own_result_authorizer,
                    family=F3OwnResultAuthorizer(
                        thin=residence_family.authority.thin,
                        configuration=residence_family.configuration,
                        clock=lambda: datetime.now(UTC),
                    ),
                ),
            )
            residence_authority = create_residence_authority(
                sessions=sessionmaker(bind=reader_engine, expire_on_commit=False, autoflush=False),
                configuration=configuration.residence,
            )
            quality = ResidenceQualityBridge(
                f2_guard=residence_directory.guard,
                retained_f2_guard=directory.guard,
                residence_authority=residence_authority,
                projection=SqlResidenceProjection(
                    sessionmaker(bind=reader_engine, expire_on_commit=False, autoflush=False)
                ),
                owner_authorizer=ResidenceQualityOwnerAuthorizer(
                    f2_guard=residence_directory.guard,
                    family_authority=residence_family.authority,
                    clock=lambda: datetime.now(UTC),
                ),
            )
            final_guard = ResidenceWriterDispatch(
                residence=ResidenceFinalGuard(
                    residence_authority, enabled=configuration.f6_features_enabled
                ),
                retained=final_guard,
                dependency=require_residence_dependencies,
                quality=ResidenceQualityFinalGuard(
                    quality, enabled=configuration.f6_features_enabled
                ),
            )
            own_result_authorizer = ResidenceOwnResultAuthorizer(
                residence=residence_authority.own_result,
                retained=own_result_authorizer,
                original_authority=residence_authority,
                quality=quality,
                dependency=residence_dependency_receipts,
            )
        package_final_guard = None
        if (
            writer_settings.schema_contract
            in {
                "school-facts-f7-v47",
                "school-facts-f7-v48",
                "school-facts-f7-v49",
                "school-facts-f7-v50",
                "school-facts-f7-v51",
                "school-facts-f7-v52",
                "school-facts-f7-v53",
                "school-facts-f7-v54",
                "school-facts-f7-v55",
                "school-facts-f7-v56", "school-facts-live-v57",
            }
            and family is not None
            and directory is not None
        ):
            family_quality_authorizer = FamilyQualityAuthorizer(directory.guard, family)
            family_quality_existing = FamilyQualityExistingContent(
                f2_guard=directory.guard,
                authorizer=family_quality_authorizer,
                projection=SqlFamilyQualityProjection(
                    sessionmaker(bind=reader_engine, expire_on_commit=False, autoflush=False)
                ),
                clock=lambda: datetime.now(UTC),
            )
            family_quality_changes = FamilyQualityChangeGuard(
                family_quality_authorizer,
                clock=lambda: datetime.now(UTC),
                existing_content=family_quality_existing,
            )
            existing_quality_kind = (
                PackageExistingQualityKind(
                    f2_guard=directory.guard,
                    sessions=family_quality_existing.projection.sessions,
                ).target_kind
                if writer_settings.schema_contract in {
                    "school-facts-f7-v54", "school-facts-f7-v55",
                    "school-facts-f7-v56", "school-facts-live-v57",
                }
                else family_quality_existing.target_kind
            )
            final_guard = FamilyQualityWriterDispatch(
                FamilyQualityFinalGuard(family_quality_changes),
                final_guard,
                existing_target_kind=existing_quality_kind,
            )
            package_guards = {
                "directory": directory.guard,
                "family": FamilyFinalAuthorizationGuard(
                    family.change_set_guard, enabled=configuration.family.enabled
                ),
                "family_quality": FamilyQualityFinalGuard(family_quality_changes),
            }
            if configuration.education is not None:
                package_guards.update(
                    {
                        "education": EducationFinalGuard(education_authority),
                        "education_quality": EducationDependencyFinalGuard(
                            quality=education_components.quality, retained=directory.guard
                        ),
                    }
                )
            if residence_authority is not None:
                package_guards.update(
                    {
                        "residence": ResidenceFinalGuard(
                            residence_authority, enabled=configuration.f6_features_enabled
                        ),
                        "residence_quality": ResidenceQualityFinalGuard(
                            quality, enabled=configuration.f6_features_enabled
                        ),
                    }
                )
            package_partition = PackageFactPartition(
                directory_configuration=configuration.directory,
                family_configuration=configuration.family,
                education_configuration=configuration.education,
                existing_quality_kind=existing_quality_kind,
                live_directory_guard=directory.guard,
            )
            family_quality_current = FamilyQualityCurrentContent(
                f2_guard=directory.guard,
                authorizer=family_quality_authorizer,
                projection=family_quality_existing.projection,
                reader=FamilyQualityReader(
                    authorizer=family_quality_authorizer,
                    executor=F2ConnectionReadExecutor(
                        family_quality_existing.projection.sessions, family.connection_authority
                    ),
                    current_version=family_quality_existing.projection.current_version,
                    clock=lambda: datetime.now(UTC),
                ),
                clock=lambda: datetime.now(UTC),
            )
            package_final_guard = PackageWriterAuthorization(
                package_partition,
                package_guards,
                references=PackageReferenceAuthorization(
                    package_partition,
                    directory_guard=directory.guard,
                    family_authority=family,
                    education_authority=education_authority,
                    residence_authority=residence_authority,
                    quality_reads=package_quality_read_proofs(
                        education=(
                            education_components.quality
                            if configuration.education is not None
                            else None
                        ),
                        residence=quality if residence_authority is not None else None,
                        family=lambda actor, _kind, key, _at: family_quality_current(actor, key)[
                            "proof"
                        ],
                    ),
                ),
            )
            if not configuration.f7_packages_enabled:

                def refuse_new_package_confirmation(session, context, change_set):
                    raise SchoolFactChangeRejected("全范围导入已停用，不能确认新的导入变更。")

                package_final_guard = refuse_new_package_confirmation
        writer_engine = build_school_fact_writer_engine(writer_settings)
        verify_school_fact_writer_database(writer_engine, settings=writer_settings)
        kernel = SqlAlchemySchoolFactWriterExecutorKernel(
            engine=writer_engine,
            settings=writer_settings,
            final_authorization_guard=final_guard,
            package_final_authorization_guard=package_final_guard,
            own_result_authorizer=own_result_authorizer,
            own_terminal_authorizer=own_terminal_authorizer,
            family_actor_validator=family_actor_validator,
        )
        correction_kernel = None
        if (
            configuration.family is not None
            and configuration.family.enabled
            and writer_settings.schema_contract
            in {
                "school-facts-f3-v27",
                "school-facts-f4-v29",
                "school-facts-f4-v30",
                "school-facts-f4-v31",
                "school-facts-f5-v34",
                "school-facts-f5-v35",
                "school-facts-f5-v36",
                "school-facts-f5-v37",
                "school-facts-f5-v38",
                "school-facts-f5-v39",
                "school-facts-f5-v40",
                "school-facts-f5-v41",
                "school-facts-f6-v44",
                "school-facts-f6-v45",
                "school-facts-f7-v47",
                "school-facts-f7-v48",
                "school-facts-f7-v49",
                "school-facts-f7-v50",
                "school-facts-f7-v51",
                "school-facts-f7-v52",
                "school-facts-f7-v53",
                "school-facts-f7-v54",
                "school-facts-f7-v55",
                "school-facts-f7-v56", "school-facts-live-v57",
            }
            and family is not None
        ):
            correction_kernel = create_family_correction_kernel(
                writer_kernel=kernel,
                family_authority=family,
                education_authority=education_authority,
                reference_key=writer_settings.context_transport_hmac_key.get_secret_value().encode(),
                clock=lambda: datetime.now(UTC),
                f2_authority=DirectoryCorrectionAuthority(directory.guard)
                if directory is not None
                else None,
                leadership_policy=(
                    lambda: cast(FamilyLeadershipPolicy, configuration.family_leadership)
                )
                if configuration.family_leadership is not None and configuration.f5_features_enabled
                else None,
                f5_enabled=writer_settings.schema_contract
                in {
                    "school-facts-f5-v41",
                    "school-facts-f6-v44",
                    "school-facts-f6-v45",
                    "school-facts-f7-v47",
                    "school-facts-f7-v48",
                    "school-facts-f7-v49",
                    "school-facts-f7-v50",
                    "school-facts-f7-v51",
                    "school-facts-f7-v52",
                    "school-facts-f7-v53",
                    "school-facts-f7-v54",
                    "school-facts-f7-v55",
                    "school-facts-f7-v56", "school-facts-live-v57",
                }
                and configuration.f5_features_enabled,
                f6_enabled=writer_settings.schema_contract
                in {
                    "school-facts-f6-v44",
                    "school-facts-f6-v45",
                    "school-facts-f7-v47",
                    "school-facts-f7-v48",
                    "school-facts-f7-v49",
                    "school-facts-f7-v50",
                    "school-facts-f7-v51",
                    "school-facts-f7-v52",
                    "school-facts-f7-v53",
                    "school-facts-f7-v54",
                    "school-facts-f7-v55",
                    "school-facts-f7-v56", "school-facts-live-v57",
                }
                and configuration.f6_features_enabled,
                residence_authority=residence_authority,
                review_commands_enabled=configuration.f5_features_enabled,
                review_enabled=writer_settings.schema_contract
                in {
                    "school-facts-f5-v35",
                    "school-facts-f5-v36",
                    "school-facts-f5-v37",
                    "school-facts-f5-v38",
                    "school-facts-f5-v39",
                    "school-facts-f5-v40",
                    "school-facts-f5-v41",
                    "school-facts-f6-v44",
                    "school-facts-f6-v45",
                    "school-facts-f7-v47",
                    "school-facts-f7-v48",
                    "school-facts-f7-v49",
                    "school-facts-f7-v50",
                    "school-facts-f7-v51",
                    "school-facts-f7-v52",
                    "school-facts-f7-v53",
                    "school-facts-f7-v54",
                    "school-facts-f7-v55",
                    "school-facts-f7-v56", "school-facts-live-v57",
                },
            )
    except BaseException:
        reader_engine.dispose()
        if writer_engine is not None:
            writer_engine.dispose()
        raise

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            reader_engine.dispose()
            writer_engine.dispose()

    app = create_school_fact_writer_executor_app(
        kernel=kernel,
        context_codec=CommandContextTransportCodec(writer_settings.context_transport_hmac_key),
        caller_auth_key=writer_settings.caller_auth_key.get_secret_value(),
        lifespan=lifespan,
        enable_own_results=configuration.directory is not None,
        correction_kernel=correction_kernel,
    )

    @app.get("/internal/v1/school-facts/features", include_in_schema=False)
    def writer_features() -> dict[str, str | bool]:
        """Local runtime contract probe; exposes no credential or authority details."""
        return {
            "schema_contract": writer_settings.schema_contract,
            "f7_packages_enabled": configuration.f7_packages_enabled,
        }

    return app
