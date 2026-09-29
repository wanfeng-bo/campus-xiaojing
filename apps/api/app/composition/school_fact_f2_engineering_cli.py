"""Finite deployment-only F2 command manifests. No HTTP or School Facts writes."""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_engineering_governance import (
    EngineeringApproval,
    EngineeringCommandAuthorizer,
    EngineeringGovernanceService,
    governance_command_digest,
    governance_command_payload,
)
from app.domains.campus import models as _campus_models  # noqa: F401
from app.domains.students import models as _student_models  # noqa: F401
from app.platform.directory import models as _directory_models  # noqa: F401
from app.platform.governance.contracts import (
    CreateDutyCommand,
    CreateGrantCommand,
    CreateScopeCommand,
    GovernanceActorContext,
    GovernanceCommand,
    RevokeGrantCommand,
)
from app.platform.idempotency import models as _idempotency_models  # noqa: F401
from app.platform.identity import models as _identity_models  # noqa: F401
from app.platform.master_data_imports import models as _import_models  # noqa: F401
from app.platform.organization import models as _organization_models  # noqa: F401
from app.platform.school_facts.f2_policy import F2_FACT_KINDS, F2FactScope
from app.platform.tenancy import models as _tenancy_models  # noqa: F401

# Deployment composition registers FK targets; Governance core never imports fact ORM.
_ORM_REGISTRATIONS = (
    _campus_models,
    _student_models,
    _directory_models,
    _idempotency_models,
    _identity_models,
    _import_models,
    _organization_models,
    _tenancy_models,
)


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class CommandInput(StrictInput):
    key: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9:_-]+$")
    effective_from: AwareDatetime


class DutyInput(CommandInput):
    command: Literal["duty"]
    duty_code: str
    display_name: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def finite(self):
        if self.duty_code not in {
            f"f2_{k}_{p}" for k in F2_FACT_KINDS for p in ("reader", "maintainer")
        }:
            raise ValueError("Only approved F2 duties are supported")
        return self


class ScopeInput(CommandInput):
    command: Literal["scope"]
    fact_kind: str
    object_keys: tuple[str, ...] = ()
    creation_keys: tuple[str, ...] = ()


class GrantInput(CommandInput):
    command: Literal["grant"]
    assignment_id: UUID
    duty_id: UUID
    duty_version: int = Field(strict=True, ge=1)
    scope_id: UUID
    scope_version: int = Field(strict=True, ge=1)
    valid_from: AwareDatetime
    planned_valid_to: AwareDatetime


class RevokeInput(CommandInput):
    command: Literal["revoke"]
    grant_id: UUID
    expected_version: int = Field(strict=True, ge=1)


Command = Annotated[
    DutyInput | ScopeInput | GrantInput | RevokeInput, Field(discriminator="command")
]


class EngineeringManifest(StrictInput):
    manifest_version: Literal["f2_governance_v1", "f2_governance_v2"]
    school_id: UUID
    actor_reference: str = Field(min_length=1, max_length=200)
    valid_from: AwareDatetime
    valid_to: AwareDatetime
    grant_valid_to: AwareDatetime | None = None
    commands: tuple[Command, ...] = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def finite(self):
        if not self.valid_from < self.valid_to or (self.valid_to - self.valid_from).days > 31:
            raise ValueError("Finite F2 window required")
        if self.manifest_version == "f2_governance_v1":
            if self.grant_valid_to is not None:
                raise ValueError("Legacy approval cannot carry a separate grant cap")
        elif self.grant_valid_to is None or self.grant_valid_to <= self.valid_from:
            raise ValueError("Explicit finite grant cap required for v2")
        grant_cap = self.grant_valid_to or self.valid_to
        if len({c.key for c in self.commands}) != len(self.commands):
            raise ValueError("Duplicate command key")
        for c in self.commands:
            if not self.valid_from <= c.effective_from < self.valid_to:
                raise ValueError("Command outside approval window")
            if isinstance(c, ScopeInput):
                F2FactScope(self.school_id, c.fact_kind, c.object_keys, c.creation_keys)
            if isinstance(c, GrantInput):
                if (
                    str(c.assignment_id) == self.actor_reference
                    or not self.valid_from <= c.valid_from < c.planned_valid_to <= grant_cap
                ):
                    raise ValueError("Self grant or grant outside approval window")
        return self

    def materialize(self) -> tuple[GovernanceCommand, ...]:
        actor = GovernanceActorContext(
            self.school_id,
            "f2_engineering",
            self.actor_reference,
            None,
            None,
            uuid4(),
            "Approved finite synthetic F2 engineering",
        )
        commands: list[GovernanceCommand] = []
        for c in self.commands:
            if isinstance(c, DutyInput):
                cmd = CreateDutyCommand(
                    school_id=self.school_id,
                    actor=actor,
                    idempotency_key=f"f2:{c.key}",
                    duty_code=c.duty_code,
                    display_name=c.display_name,
                    description=None,
                    effective_from=c.effective_from,
                )
            elif isinstance(c, ScopeInput):
                scope = F2FactScope(self.school_id, c.fact_kind, c.object_keys, c.creation_keys)
                cmd = CreateScopeCommand(
                    school_id=self.school_id,
                    actor=actor,
                    idempotency_key=f"f2:{c.key}",
                    scope_key=c.key,
                    schema_owner="school_facts.f2",
                    schema_name="exact_fact_set",
                    schema_version=1,
                    reference_document=scope.document,
                    reference_sha256=scope.document_sha256,
                    effective_from=c.effective_from,
                )
            elif isinstance(c, GrantInput):
                cmd = CreateGrantCommand(
                    school_id=self.school_id,
                    actor=actor,
                    idempotency_key=f"f2:{c.key}",
                    assignment_id=c.assignment_id,
                    duty_id=c.duty_id,
                    duty_version=c.duty_version,
                    scope_id=c.scope_id,
                    scope_version=c.scope_version,
                    basis_authority="f2_engineering_policy",
                    basis_reference=c.key,
                    valid_from=c.valid_from,
                    effective_from=c.effective_from,
                    planned_valid_to=c.planned_valid_to,
                )
            else:
                cmd = RevokeGrantCommand(
                    school_id=self.school_id,
                    actor=actor,
                    idempotency_key=f"f2:{c.key}",
                    grant_id=c.grant_id,
                    expected_version=c.expected_version,
                    effective_at=c.effective_from,
                )
            commands.append(cmd)
        return tuple(commands)

    def approval(self) -> EngineeringApproval:
        return EngineeringApproval(
            self.school_id,
            self.actor_reference,
            self.valid_from,
            self.valid_to,
            frozenset(governance_command_digest(c) for c in self.materialize()),
            grant_valid_to=self.grant_valid_to,
        )


class EngineeringPrivateConfiguration(StrictInput):
    approved_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor_reference: str
    credential: SecretStr = Field(min_length=16)
    operator_assignment_id: UUID | None = None
    database_host: str
    database_port: int = Field(ge=1, le=65535)
    database_name: str
    database_user: Literal["campus_ai_governance_writer"]
    database_password: SecretStr
    expected_anchor_id: UUID
    expected_school_id: UUID
    expected_system_identifier: str
    reader: dict[str, object]


def load_manifest(path: Path, expected_digest: str) -> EngineeringManifest:
    data = path.read_bytes()
    if len(data) > 1_048_576 or not hmac.compare_digest(sha256(data).hexdigest(), expected_digest):
        raise ValueError("Missing or changed approved manifest")
    return EngineeringManifest.model_validate_json(data)


def command_marker(manifest_digest: str, command: GovernanceCommand) -> str:
    return f"CONFIRM-F2:{manifest_digest}:{governance_command_digest(command)}"


def review(manifest: EngineeringManifest, digest: str) -> dict[str, object]:
    return {
        "manifest_sha256": digest,
        "expires_at": manifest.valid_to.isoformat(),
        "manifest_version": manifest.manifest_version,
        "grant_valid_to": (manifest.grant_valid_to or manifest.valid_to).isoformat(),
        "commands": [
            {
                "index": i,
                "payload": json.loads(governance_command_payload(c)),
                "command_digest": governance_command_digest(c),
                "confirm_marker": command_marker(digest, c),
            }
            for i, c in enumerate(manifest.materialize())
        ],
    }


class GrantBinding(StrictInput):
    key: str
    assignment_id: UUID
    duty_key: str
    scope_key: str


def build_grants(
    manifest: EngineeringManifest,
    bindings: list[GrantBinding],
    *,
    service: EngineeringGovernanceService,
    credential: str,
) -> EngineeringManifest:
    """Resolve actual committed IDs; output must itself be reviewed and pinned before execution."""
    if not bindings:
        raise ValueError("Explicit assignment/duty/scope bindings required")
    commands = dict(zip((c.key for c in manifest.commands), manifest.materialize(), strict=True))
    grants: list[GrantInput] = []
    for b in bindings:
        duty, scope = commands.get(b.duty_key), commands.get(b.scope_key)
        if not isinstance(duty, CreateDutyCommand) or not isinstance(scope, CreateScopeCommand):
            raise ValueError("Grant references must name approved duty and scope commands")
        fact_scope = F2FactScope.from_document(scope.reference_document)
        if duty.duty_code not in {
            f"f2_{fact_scope.fact_kind}_reader",
            f"f2_{fact_scope.fact_kind}_maintainer",
        }:
            raise ValueError("Duty and scope family must match")
        duty_receipt = service.receipt(duty, credential=credential)
        scope_receipt = service.receipt(scope, credential=credential)
        if duty_receipt is None or scope_receipt is None:
            raise ValueError("Committed authoritative prerequisite receipts required")
        grants.append(
            GrantInput(
                command="grant",
                key=b.key,
                assignment_id=b.assignment_id,
                duty_id=duty_receipt.target_id,
                duty_version=duty_receipt.target_version,
                scope_id=scope_receipt.target_id,
                scope_version=scope_receipt.target_version,
                valid_from=manifest.valid_from,
                effective_from=manifest.valid_from,
                planned_valid_to=manifest.grant_valid_to or manifest.valid_to,
            )
        )
    return EngineeringManifest.model_validate({**manifest.model_dump(), "commands": tuple(grants)})


def compose(
    config: EngineeringPrivateConfiguration,
    manifest: EngineeringManifest,
    *,
    approval_is_current: Callable[[], bool] | None = None,
):
    from app.composition.school_fact_directory_services import F2ScopeOwnerLookup
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.composition.school_fact_reader import (
        SchoolFactReaderSettings,
        build_school_fact_reader_engine,
        verify_school_fact_reader_database,
    )
    from app.platform.governance.application import GovernanceWriter
    from app.platform.school_facts.f2_policy import F2ScopeResolver

    class ExplicitReader(SchoolFactReaderSettings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ):
            return (init_settings,)

    if (
        manifest.school_id != config.expected_school_id
        or manifest.actor_reference != config.actor_reference
    ):
        raise ValueError("Manifest identity does not match independent operator configuration")
    if any(
        isinstance(c, GrantInput) and c.assignment_id == config.operator_assignment_id
        for c in manifest.commands
    ):
        raise ValueError("Operator cannot grant itself product authority")
    reader = ExplicitReader.model_validate(config.reader)
    if reader.schema_contract != "school-facts-f2-v25" or (
        reader.database_host,
        reader.database_port,
        reader.database_name,
        reader.expected_anchor_id,
        reader.expected_school_id,
        reader.expected_system_identifier,
    ) != (
        config.database_host,
        config.database_port,
        config.database_name,
        config.expected_anchor_id,
        config.expected_school_id,
        config.expected_system_identifier,
    ):
        raise ValueError("Separate Reader must bind the same exact F2 database")
    reader_engine = build_school_fact_reader_engine(reader)
    writer_engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username=config.database_user,
            password=config.database_password.get_secret_value(),
            host=config.database_host,
            port=config.database_port,
            database=config.database_name,
        ),
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=10,
        pool_recycle=300,
        hide_parameters=True,
        connect_args={"options": "-c statement_timeout=30000 -c lock_timeout=3000"},
    )
    try:
        verify_school_fact_reader_database(reader_engine, settings=reader)
        with writer_engine.connect() as connection:
            role = connection.execute(
                text(
                    "SELECT current_user,session_user,rolsuper,rolbypassrls "
                    "FROM pg_roles WHERE rolname=current_user"
                )
            ).one()
            if role != (config.database_user, config.database_user, False, False):
                raise ValueError("Isolated Governance role rejected")
            connection.execute(
                text("SELECT app.assert_school_database_anchor(:anchor,:school,:system)"),
                {
                    "anchor": config.expected_anchor_id,
                    "school": config.expected_school_id,
                    "system": config.expected_system_identifier,
                },
            ).scalar_one()
        sessions = sessionmaker(reader_engine, expire_on_commit=False)

        def clock():
            return datetime.now(UTC)

        approval = manifest.approval()
        authorizer = EngineeringCommandAuthorizer(
            approval=approval, clock=clock, approval_is_current=approval_is_current
        )
        writer = GovernanceWriter(
            session_factory=sessionmaker(writer_engine, expire_on_commit=False),
            authorizer=authorizer,
            transaction_authorizer=authorizer,
            clock=clock,
            dependency_port=ReadOnlyGovernanceDependencies(sessions),
            scope_owner_resolvers={
                "school_facts.f2": F2ScopeResolver(lookup=F2ScopeOwnerLookup(sessions))
            },
        )
        return EngineeringGovernanceService(
            writer=writer,
            approval=approval,
            credential=config.credential.get_secret_value(),
            clock=clock,
            approval_is_current=approval_is_current,
        ), (writer_engine, reader_engine)
    except BaseException:
        writer_engine.dispose()
        reader_engine.dispose()
        raise


class ScopeMatrixRow(StrictInput):
    key: str
    fact_kind: str
    purpose: Literal["reader", "maintainer"]
    object_keys: tuple[str, ...] = ()
    creation_keys: tuple[str, ...] = ()


def build_duties_and_scopes(
    *,
    school_id: UUID,
    actor_reference: str,
    valid_from: datetime,
    valid_to: datetime,
    rows: tuple[ScopeMatrixRow, ...],
) -> EngineeringManifest:
    """Build only the finite reviewed matrix; grants are a later receipt-derived manifest."""
    commands: list[DutyInput | ScopeInput] = []
    duties: set[str] = set()
    for row in rows:
        scope = F2FactScope(school_id, row.fact_kind, row.object_keys, row.creation_keys)
        if row.purpose == "reader" and row.creation_keys:
            raise ValueError("Reader authority cannot contain creation keys")
        code = f"f2_{row.fact_kind}_{row.purpose}"
        if code not in duties:
            commands.append(
                DutyInput(
                    command="duty",
                    key=code,
                    duty_code=code,
                    display_name=f"F2 synthetic {row.fact_kind} {row.purpose}",
                    effective_from=valid_from,
                )
            )
            duties.add(code)
        commands.append(
            ScopeInput(
                command="scope",
                key=row.key,
                fact_kind=row.fact_kind,
                object_keys=scope.object_keys,
                creation_keys=scope.creation_keys,
                effective_from=valid_from,
            )
        )
    return EngineeringManifest(
        manifest_version="f2_governance_v1",
        school_id=school_id,
        actor_reference=actor_reference,
        valid_from=valid_from,
        valid_to=valid_to,
        commands=tuple(commands),
    )
