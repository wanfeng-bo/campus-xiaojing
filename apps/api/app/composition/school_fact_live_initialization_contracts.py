"""Reviewed finite inputs for the existing first-maintainer deployment boundary.

References record the operator's reviewed authority; possession of this document or
database credentials does not establish that authority. No values are generated here.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    SecretStr,
    StringConstraints,
    model_validator,
)
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from app.composition.school_fact_product_writer import (
    ExplicitReaderSettings,
    ExplicitWriterSettings,
)
from app.core.settings import Settings
from app.db.migration_settings import MigrationSettings
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from app.platform.school_facts.initialization_contracts import SchoolFactInitializationRequest

Reference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
LiveDatabase = Annotated[
    str, StringConstraints(pattern=r"^campus_ai_school_facts_live(?:_[a-z0-9]+)*$", max_length=63)
]
CONTACT_FIELDS = (
    "owner_kind",
    "organization_source_key",
    "location_source_key",
    "contact_kind",
    "contact_value",
    "service_hours",
    "lifecycle_status",
    "visibility_scope",
    "audience_code",
)
CONTACT_DUTIES = ("official_contact_reader", "official_contact_maintainer")


class ReviewedDeployment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    approval_reference: Reference
    issuer_reference: Reference
    issuer_qualification_reference: Reference
    operator_reference: Reference
    source_authority_reference: Reference
    approved_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def finite(self) -> Self:
        if not timedelta(0) < self.expires_at - self.approved_at <= timedelta(minutes=15):
            raise ValueError("Deployment approval must be a fixed window of at most 15 minutes.")
        return self

    def require_current(self, now: datetime) -> None:
        if now.utcoffset() is None or not self.approved_at <= now < self.expires_at:
            raise ValueError("Reviewed deployment approval window is closed.")

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class LiveRootManifest(ReviewedDeployment):
    purpose: Literal["school-facts-live-root"]
    db: LiveDatabase
    system_identifier: Annotated[str, StringConstraints(pattern=r"^[0-9]{10,32}$")]
    school_id: UUID
    source_key: Reference
    school_code: Reference
    display_name: Reference
    timezone_name: Literal["Asia/Shanghai"]

    @model_validator(mode="after")
    def non_nil_school(self) -> Self:
        if self.school_id.int == 0:
            raise ValueError("A non-nil reviewed school identifier is required.")
        return self


class LiveSessionHandoffApproval(ReviewedDeployment):
    """Approve only identity delivery before the school's first facts are prepared."""

    purpose: Literal["school-facts-live-session-handoff"]
    school_id: UUID
    anchor_id: UUID
    expected_system_identifier: Annotated[str, StringConstraints(pattern=r"^[0-9]{10,32}$")]
    database_name: LiveDatabase
    corp_id: Reference
    external_subject: Reference

    @model_validator(mode="after")
    def non_nil_target(self) -> Self:
        if self.school_id.int == 0 or self.anchor_id.int == 0:
            raise ValueError("A reviewed school and anchor are required for identity delivery.")
        return self


class LiveInitializationManifest(ReviewedDeployment):
    purpose: Literal["school-facts-live-initialization"]
    school_id: UUID
    anchor_id: UUID
    expected_system_identifier: Annotated[str, StringConstraints(pattern=r"^[0-9]{10,32}$")]
    database_name: LiveDatabase
    request_id: UUID
    corp_id: Reference
    external_subject: Reference
    grant_valid_from: AwareDatetime
    grant_valid_to: AwareDatetime
    scope_school_id: UUID
    duties: tuple[str, ...]
    fields: tuple[str, ...]
    first_facts: CanonicalFactChangeSetV1
    publication_policy: Literal["required", "deferred"] = "required"

    @property
    def manifest_sha256(self) -> str:
        # The default contract predates this field. Preserve its exact reviewed
        # digest and original receipts; a deferred decision must have a new digest.
        excluded = {"publication_policy"} if self.publication_policy == "required" else set()
        return hashlib.sha256(self.model_dump_json(exclude=excluded).encode()).hexdigest()

    @model_validator(mode="after")
    def fixed_initial_scope(self) -> Self:
        if (
            self.scope_school_id != self.school_id
            or self.duties != CONTACT_DUTIES
            or self.fields != CONTACT_FIELDS
            or self.grant_valid_from != self.approved_at
            or self.grant_valid_to != self.grant_valid_from + timedelta(days=90)
            or any(c.effective_at != self.approved_at for c in self.first_facts.changes)
            or any(x.int == 0 for x in (self.school_id, self.anchor_id, self.request_id))
        ):
            raise ValueError(
                "Only the reviewed school/contact fields and fixed 90-day duties are supported."
            )
        request = self.initialization_request()
        mapping = next(
            c.content for c in request.change_set.changes if c.fact_kind == "login_mapping"
        )
        if mapping is None or (
            mapping.provider_code,
            mapping.provider_tenant_key,
            mapping.external_subject,
        ) != ("dingtalk", self.corp_id, self.external_subject):
            raise ValueError("First facts must bind exactly the reviewed DingTalk identity.")
        return self

    def initialization_request(self) -> SchoolFactInitializationRequest:
        return SchoolFactInitializationRequest(
            school_id=self.school_id,
            anchor_id=self.anchor_id,
            expected_system_identifier=self.expected_system_identifier,
            request_id=self.request_id,
            approval_reference="live-init:" + self.manifest_sha256,
            operator_reference=self.operator_reference,
            change_set=self.first_facts,
        )


class _Explicit:
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


class LiveInitializationMigration(_Explicit, MigrationSettings):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    db: LiveDatabase

    @model_validator(mode="after")
    def migration_principal(self) -> Self:
        if self.user != "campus_ai_migration":
            raise ValueError("Initialization requires the migration principal.")
        return self


class LiveInitializationRuntime(_Explicit, Settings):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    postgres_db: LiveDatabase

    @model_validator(mode="after")
    def formal_runtime(self) -> Self:
        if (
            self.app_env != "production"
            or self.identity_provider != "dingtalk"
            or self.postgres_user != "campus_ai_runtime"
            or self.postgres_schema_contract != "school-facts-f1-v22"
            or self.identity_session_cookie_name != "school_fact_live_session"
            or self.repair_draft_model_enabled
            or self.repair_draft_model_api_key is not None
        ):
            raise ValueError("Initialization requires explicit formal schema22 identity settings.")
        return self


class LiveInitializationSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    token: SecretStr
    corp_id: Reference
    external_subject: Reference
    # Display-only input for a later reviewed person fact, never an identity or
    # authorization proof. Consumers revalidate token/corp/subject in Identity.
    display_name_candidate: Reference | None = None


class LiveInitializationConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    purpose: Literal["school-facts-live-initialization"]
    migration: LiveInitializationMigration
    runtime: LiveInitializationRuntime
    reader: ExplicitReaderSettings
    writer: ExplicitWriterSettings

    @model_validator(mode="after")
    def matching_components(self) -> Self:
        r, m = self.runtime, self.migration
        expected = (
            r.postgres_host,
            r.postgres_port,
            r.postgres_db,
            r.postgres_expected_anchor_id,
            r.postgres_expected_school_id,
            r.postgres_expected_system_identifier,
        )
        if (m.host, m.port, m.db) != expected[:3]:
            raise ValueError("Migration and runtime must target one explicit new database.")
        for c in (self.reader, self.writer):
            if (
                c.database_host,
                c.database_port,
                c.database_name,
                c.expected_anchor_id,
                c.expected_school_id,
                c.expected_system_identifier,
            ) != expected or c.schema_contract != "school-facts-f1-v22":
                raise ValueError("Initialization components must match the same schema22 anchor.")
        secrets = (
            m.password.get_secret_value(),
            r.postgres_password,
            r.redis_password,
            self.reader.database_password.get_secret_value(),
            self.writer.database_password.get_secret_value(),
        )
        if any(len(x) < 32 for x in secrets) or len(set(secrets)) != len(secrets):
            raise ValueError("Initialization requires independent strong component credentials.")
        if m.context_signing_key != r.rls_context_signing_key:
            raise ValueError("Reviewed migration/runtime context keys must agree.")
        return self

    def require_manifest(self, manifest: LiveInitializationManifest) -> None:
        r = self.runtime
        if (
            manifest.database_name,
            manifest.school_id,
            manifest.anchor_id,
            manifest.expected_system_identifier,
            manifest.corp_id,
        ) != (
            r.postgres_db,
            r.postgres_expected_school_id,
            r.postgres_expected_anchor_id,
            r.postgres_expected_system_identifier,
            r.dingtalk_corp_id,
        ):
            raise ValueError("Reviewed manifest does not match the isolated configuration.")
