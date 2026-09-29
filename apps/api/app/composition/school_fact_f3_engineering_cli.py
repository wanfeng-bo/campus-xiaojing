"""Finite deployment-only F3 command manifests. No HTTP or School Facts writes."""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_f3_engineering_governance import (
    F3EngineeringApproval,
    F3EngineeringCommandAuthorizer,
    F3EngineeringGovernanceService,
    f3_governance_command_digest,
    f3_governance_command_payload,
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
from app.platform.school_facts.f3_policy import F3_FACT_KINDS, F3FactScope
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
        allowed = {f"f3_{k}_{p}" for k in F3_FACT_KINDS for p in ("reader", "maintainer")} | {
            "f3_guardian_relationship_recorder",
            "f3_guardian_relationship_verifier",
        }
        if self.duty_code not in allowed:
            raise ValueError("Only approved F3 duties are supported")
        return self


class ScopeInput(CommandInput):
    command: Literal["scope"]
    fact_kind: str
    object_keys: tuple[str, ...] = ()
    creation_keys: tuple[str, ...] = ()
    field_names: tuple[str, ...] = Field(min_length=1)


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
    manifest_version: Literal["f3_governance_v1"]
    school_id: UUID
    actor_reference: str = Field(min_length=1, max_length=200)
    valid_from: AwareDatetime
    valid_to: AwareDatetime
    grant_valid_to: AwareDatetime
    approved_duty_codes: frozenset[str] = Field(min_length=1)
    approved_assignment_ids: frozenset[UUID] = Field(min_length=1)
    commands: tuple[Command, ...] = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def finite(self):
        if (
            not self.valid_from < self.valid_to
            or self.valid_to - self.valid_from > timedelta(minutes=15)
            or not self.valid_from < self.grant_valid_to
            or self.grant_valid_to > self.valid_from + timedelta(days=7)
        ):
            raise ValueError("Finite F3 execution window and grant cap required")
        allowed_duties = {
            f"f3_{kind}_{purpose}" for kind in F3_FACT_KINDS for purpose in ("reader", "maintainer")
        } | {
            "f3_guardian_relationship_recorder",
            "f3_guardian_relationship_verifier",
        }
        if not self.approved_duty_codes <= allowed_duties or len(
            {c.key for c in self.commands}
        ) != len(self.commands):
            raise ValueError("Unknown duty or duplicate command key")
        for c in self.commands:
            if isinstance(c, DutyInput) and c.duty_code not in self.approved_duty_codes:
                raise ValueError("Duty command is outside the approved duty set")
            if isinstance(c, GrantInput) and c.assignment_id not in self.approved_assignment_ids:
                raise ValueError("Grant is outside the approved assignment set")
            if not self.valid_from <= c.effective_from < self.valid_to:
                raise ValueError("Command outside approval window")
            if isinstance(c, ScopeInput):
                F3FactScope(
                    self.school_id, c.fact_kind, c.object_keys, c.creation_keys, c.field_names
                )
            if isinstance(c, GrantInput):
                if (
                    str(c.assignment_id) == self.actor_reference
                    or not self.valid_from
                    <= c.valid_from
                    < c.planned_valid_to
                    <= self.grant_valid_to
                ):
                    raise ValueError("Self grant, backdated grant, or grant outside fixed cap")
        return self

    def materialize(self) -> tuple[GovernanceCommand, ...]:
        actor = GovernanceActorContext(
            self.school_id,
            "f3_engineering",
            self.actor_reference,
            None,
            None,
            uuid4(),
            "Approved finite synthetic F3 engineering",
        )
        commands: list[GovernanceCommand] = []
        for c in self.commands:
            if isinstance(c, DutyInput):
                cmd = CreateDutyCommand(
                    school_id=self.school_id,
                    actor=actor,
                    idempotency_key=f"f3:{c.key}",
                    duty_code=c.duty_code,
                    display_name=c.display_name,
                    description=None,
                    effective_from=c.effective_from,
                )
            elif isinstance(c, ScopeInput):
                scope = F3FactScope(
                    self.school_id, c.fact_kind, c.object_keys, c.creation_keys, c.field_names
                )
                cmd = CreateScopeCommand(
                    school_id=self.school_id,
                    actor=actor,
                    idempotency_key=f"f3:{c.key}",
                    scope_key=c.key,
                    schema_owner="school_facts.f3",
                    schema_name="exact_family_fact_set",
                    schema_version=1,
                    reference_document=scope.document,
                    reference_sha256=scope.document_sha256,
                    effective_from=c.effective_from,
                )
            elif isinstance(c, GrantInput):
                cmd = CreateGrantCommand(
                    school_id=self.school_id,
                    actor=actor,
                    idempotency_key=f"f3:{c.key}",
                    assignment_id=c.assignment_id,
                    duty_id=c.duty_id,
                    duty_version=c.duty_version,
                    scope_id=c.scope_id,
                    scope_version=c.scope_version,
                    basis_authority="f3_engineering_policy",
                    basis_reference=c.key,
                    valid_from=c.valid_from,
                    effective_from=c.effective_from,
                    planned_valid_to=c.planned_valid_to,
                )
            else:
                cmd = RevokeGrantCommand(
                    school_id=self.school_id,
                    actor=actor,
                    idempotency_key=f"f3:{c.key}",
                    grant_id=c.grant_id,
                    expected_version=c.expected_version,
                    effective_at=c.effective_from,
                )
            commands.append(cmd)
        return tuple(commands)

    def approval(self) -> F3EngineeringApproval:
        commands = self.materialize()
        return F3EngineeringApproval(
            self.school_id,
            self.actor_reference,
            self.valid_from,
            self.valid_to,
            self.grant_valid_to,
            frozenset(f3_governance_command_digest(c) for c in commands),
            self.approved_duty_codes,
            self.approved_assignment_ids,
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
    return f"CONFIRM-F3:{manifest_digest}:{f3_governance_command_digest(command)}"


def review(manifest: EngineeringManifest, digest: str) -> dict[str, object]:
    return {
        "manifest_sha256": digest,
        "expires_at": manifest.valid_to.isoformat(),
        "manifest_version": manifest.manifest_version,
        "grant_valid_to": (manifest.grant_valid_to).isoformat(),
        "commands": [
            {
                "index": i,
                "payload": json.loads(f3_governance_command_payload(c)),
                "command_digest": f3_governance_command_digest(c),
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
    grant_valid_from: datetime,
    execution_valid_to: datetime,
    fixed_grant_valid_to: datetime | None = None,
    service: F3EngineeringGovernanceService,
    credential: str,
) -> EngineeringManifest:
    """Resolve actual committed IDs; output must itself be reviewed and pinned before execution."""
    if not bindings:
        raise ValueError("Explicit assignment/duty/scope bindings required")
    deadline = fixed_grant_valid_to or manifest.grant_valid_to
    if deadline != manifest.grant_valid_to:
        raise ValueError("The fixed F3 grant deadline cannot move")
    if not manifest.valid_from <= grant_valid_from < execution_valid_to <= deadline:
        raise ValueError("Later grants require their actual stage time within the fixed deadline")
    if execution_valid_to - grant_valid_from > timedelta(minutes=15):
        raise ValueError("Later grant execution window cannot exceed 15 minutes")
    commands = dict(zip((c.key for c in manifest.commands), manifest.materialize(), strict=True))
    grants: list[GrantInput] = []
    duty_codes: set[str] = set()
    for b in bindings:
        duty, scope = commands.get(b.duty_key), commands.get(b.scope_key)
        if not isinstance(duty, CreateDutyCommand) or not isinstance(scope, CreateScopeCommand):
            raise ValueError("Grant references must name approved duty and scope commands")
        fact_scope = F3FactScope.from_document(scope.reference_document)
        allowed_duties = {
            f"f3_{fact_scope.fact_kind}_reader",
            f"f3_{fact_scope.fact_kind}_maintainer",
        }
        if fact_scope.fact_kind == "guardian_relationship":
            allowed_duties.update(
                {"f3_guardian_relationship_recorder", "f3_guardian_relationship_verifier"}
            )
        if duty.duty_code not in allowed_duties:
            raise ValueError("Duty and scope family must match")
        duty_codes.add(duty.duty_code)
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
                valid_from=grant_valid_from,
                effective_from=grant_valid_from,
                planned_valid_to=deadline,
            )
        )
    return EngineeringManifest.model_validate(
        {
            **manifest.model_dump(),
            "valid_from": grant_valid_from,
            "valid_to": execution_valid_to,
            "approved_duty_codes": frozenset(duty_codes),
            "approved_assignment_ids": frozenset(b.assignment_id for b in bindings),
            "commands": tuple(grants),
        }
    )


def compose(
    config: EngineeringPrivateConfiguration,
    manifest: EngineeringManifest,
    *,
    approval_is_current: Callable[[], bool],
):
    from app.composition.school_fact_family_authority import F3ScopeOwnerLookup
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.composition.school_fact_reader import (
        SchoolFactReaderSettings,
        build_school_fact_reader_engine,
        verify_school_fact_reader_database,
    )
    from app.platform.governance.application import GovernanceWriter
    from app.platform.school_facts.f3_policy import F3ScopeResolver

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
    if reader.schema_contract != "school-facts-f3-v27" or (
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
        raise ValueError("Separate Reader must bind the same exact F3 database")
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
        authorizer = F3EngineeringCommandAuthorizer(
            approval=approval, clock=clock, approval_is_current=approval_is_current
        )
        writer = GovernanceWriter(
            session_factory=sessionmaker(writer_engine, expire_on_commit=False),
            authorizer=authorizer,
            transaction_authorizer=authorizer,
            clock=clock,
            dependency_port=ReadOnlyGovernanceDependencies(sessions),
            scope_owner_resolvers={
                "school_facts.f3": F3ScopeResolver(
                    lookup_scope=F3ScopeOwnerLookup(sessions).scope_exists
                )
            },
        )
        return F3EngineeringGovernanceService(
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
    action: Literal["read", "maintain", "request", "verify", "end", "revoke"]
    object_keys: tuple[str, ...] = ()
    creation_keys: tuple[str, ...] = ()
    field_names: tuple[str, ...] = Field(min_length=1)


def _duty_for(fact_kind: str, action: str) -> str:
    if fact_kind == "guardian_relationship" and action == "request":
        return "f3_guardian_relationship_recorder"
    if fact_kind == "guardian_relationship" and action in {"verify", "end", "revoke"}:
        return "f3_guardian_relationship_verifier"
    return f"f3_{fact_kind}_{'reader' if action == 'read' else 'maintainer'}"


def build_duties_and_scopes(
    *,
    school_id: UUID,
    actor_reference: str,
    valid_from: datetime,
    valid_to: datetime,
    grant_valid_to: datetime,
    approved_assignment_ids: frozenset[UUID],
    rows: tuple[ScopeMatrixRow, ...],
) -> EngineeringManifest:
    """Build finite F3 duty/scope commands; grants require committed receipts."""
    commands: list[DutyInput | ScopeInput] = []
    duties: set[str] = set()
    for row in rows:
        scope = F3FactScope(
            school_id, row.fact_kind, row.object_keys, row.creation_keys, row.field_names
        )
        if row.action == "read" and row.creation_keys:
            raise ValueError("Reader authority cannot contain creation keys")
        if row.fact_kind != "guardian_relationship" and row.action not in {"read", "maintain"}:
            raise ValueError("Family-only action used for another fact kind")
        code = _duty_for(row.fact_kind, row.action)
        if code not in duties:
            commands.append(
                DutyInput(
                    command="duty",
                    key=code,
                    duty_code=code,
                    display_name=f"F3 synthetic {row.fact_kind} {row.action}",
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
                field_names=scope.field_names,
                effective_from=valid_from,
            )
        )
    return EngineeringManifest(
        manifest_version="f3_governance_v1",
        school_id=school_id,
        actor_reference=actor_reference,
        valid_from=valid_from,
        valid_to=valid_to,
        grant_valid_to=grant_valid_to,
        approved_duty_codes=frozenset(duties),
        approved_assignment_ids=approved_assignment_ids,
        commands=tuple(commands),
    )
