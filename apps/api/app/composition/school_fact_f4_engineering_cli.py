"""Pinned F4 manifests with explicit, narrow exceptions for existing fact owners."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from app.composition.school_fact_f3_engineering_cli import (
    CommandInput,
    GrantBinding,
    RevokeInput,
    StrictInput,
)
from app.composition.school_fact_f3_engineering_cli import (
    EngineeringPrivateConfiguration as _PrivateConfiguration,
)
from app.composition.school_fact_f3_engineering_cli import (
    GrantInput as _GrantInput,
)
from app.composition.school_fact_f4_engineering_governance import (
    F4_ALLOWED_DUTIES,
    F4EngineeringApproval,
    F4EngineeringCommandAuthorizer,
    F4EngineeringGovernanceService,
    GrantReferenceProof,
    dependency_maintenance_allowed,
    f4_governance_command_digest,
    f4_governance_command_payload,
)
from app.platform.governance.contracts import (
    CreateDutyCommand,
    CreateGrantCommand,
    CreateScopeCommand,
    GovernanceActorContext,
    GovernanceCommand,
    RevokeGrantCommand,
)
from app.platform.school_facts.f2_policy import F2FactScope
from app.platform.school_facts.f3_policy import F3FactScope


class ScopeMatrixRow(StrictInput):
    key: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9:_-]+$")
    family: Literal["f2", "f3", "f4"]
    fact_kind: str
    action: Literal["read", "maintain"]
    object_keys: tuple[str, ...] = ()
    creation_keys: tuple[str, ...] = ()
    field_names: tuple[str, ...]
    prior_scope_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def finite(self):
        if not self.field_names and (self.family, self.fact_kind) != ("f2", "policy"):
            raise ValueError("Only the fieldless policy metadata owner accepts no fields")
        if (
            self.family != "f4"
            and self.action == "maintain"
            and not dependency_maintenance_allowed(
                self.family, self.fact_kind, self.object_keys, self.creation_keys
            )
        ):
            raise ValueError("Dependency maintenance is outside the explicit F4 owner exceptions")
        if self.action == "read" and self.creation_keys:
            raise ValueError("Reader scopes cannot create objects")
        new_education_dependencies = bool(self.object_keys) and all(
            key.startswith(f"f4:{self.fact_kind}:") for key in self.object_keys
        )
        if (
            self.family == "f2"
            and self.action == "read"
            and self.prior_scope_sha256 is None
            and not new_education_dependencies
        ):
            raise ValueError("F2 requires a verified existing object-scope fingerprint")
        return self

    def scope(self, school_id: UUID) -> Any:
        if self.family == "f2":
            result = F2FactScope(school_id, self.fact_kind, self.object_keys, self.creation_keys)
            if (
                self.prior_scope_sha256 is not None
                and result.document_sha256 != self.prior_scope_sha256
            ):
                raise ValueError("F2 dependency differs from its verified original scope")
            return result
        if self.family == "f3":
            return F3FactScope(
                school_id, self.fact_kind, self.object_keys, self.creation_keys, self.field_names
            )
        from app.platform.school_facts.f4_policy import F4FactScope

        return F4FactScope(
            school_id, self.fact_kind, self.object_keys, self.creation_keys, self.field_names
        )

    @property
    def duty_code(self) -> str:
        return (
            f"{self.family}_{self.fact_kind}_{'reader' if self.action == 'read' else 'maintainer'}"
        )


class DutyInput(CommandInput):
    command: Literal["duty"]
    duty_code: str
    display_name: str = Field(min_length=1, max_length=200)


class ScopeInput(ScopeMatrixRow):
    command: Literal["scope"]
    effective_from: AwareDatetime


class GrantInput(_GrantInput):
    duty_code: str
    scope_owner: Literal["school_facts.f2", "school_facts.f3", "school_facts.f4"]
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def proof(self) -> GrantReferenceProof:
        return GrantReferenceProof(
            self.duty_id,
            self.duty_version,
            self.duty_code,
            self.scope_id,
            self.scope_version,
            self.scope_owner,
            self.scope_sha256,
        )


Command = Annotated[
    DutyInput | ScopeInput | GrantInput | RevokeInput, Field(discriminator="command")
]


class ExistingDutyReference(StrictInput):
    duty_code: str
    duty_id: UUID
    duty_version: int = Field(ge=1)


class EngineeringManifest(StrictInput):
    manifest_version: Literal["f4_governance_v1"]
    school_id: UUID
    actor_reference: str = Field(min_length=1, max_length=200)
    approval_reference: str = Field(min_length=1, max_length=200)
    valid_from: AwareDatetime
    valid_to: AwareDatetime
    grant_epoch: AwareDatetime
    grant_valid_to: AwareDatetime
    approved_duty_codes: frozenset[str] = Field(min_length=1)
    approved_assignment_ids: frozenset[UUID] = Field(min_length=1)
    commands: tuple[Command, ...] = Field(min_length=1, max_length=300)
    existing_duties: tuple[ExistingDutyReference, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def finite(self):
        prior_codes = {d.duty_code for d in self.existing_duties}
        if (
            len(prior_codes) != len(self.existing_duties)
            or not prior_codes <= self.approved_duty_codes
            or any(isinstance(c, DutyInput) and c.duty_code in prior_codes for c in self.commands)
        ):
            raise ValueError("Existing duties must be unique, approved and not recreated")
        if (
            not timedelta(0) < self.valid_to - self.valid_from <= timedelta(minutes=15)
            or self.grant_valid_to != self.grant_epoch + timedelta(days=7)
            or not self.grant_epoch <= self.valid_from < self.valid_to <= self.grant_valid_to
            or not self.approved_duty_codes <= F4_ALLOWED_DUTIES
            or len({c.key for c in self.commands}) != len(self.commands)
        ):
            raise ValueError(
                "Fixed seven-day epoch, exact duties and short unique commands required"
            )
        for c in self.commands:
            if not self.valid_from <= c.effective_from < self.valid_to:
                raise ValueError("Command is outside its finite execution window")
            if (
                isinstance(c, (DutyInput, GrantInput))
                and c.duty_code not in self.approved_duty_codes
            ):
                raise ValueError("Duty is not in this exact manifest")
            if isinstance(c, ScopeInput):
                c.scope(self.school_id)
                if c.duty_code not in self.approved_duty_codes:
                    raise ValueError("Scope duty is outside this manifest")
            if isinstance(c, GrantInput) and (
                c.assignment_id not in self.approved_assignment_ids
                or str(c.assignment_id) == self.actor_reference
                or not self.valid_from <= c.valid_from < self.valid_to
                or c.planned_valid_to != self.grant_valid_to
                or not c.duty_code.startswith(c.scope_owner.rsplit(".", 1)[1] + "_")
            ):
                raise ValueError("Grant assignment, family, start or fixed deadline rejected")
        return self

    def materialize(self) -> tuple[GovernanceCommand, ...]:
        actor = GovernanceActorContext(
            self.school_id,
            "f4_engineering",
            self.actor_reference,
            None,
            None,
            uuid4(),
            "Approved finite F4 engineering",
        )
        result: list[GovernanceCommand] = []
        for c in self.commands:
            common: dict[str, Any] = dict(
                school_id=self.school_id, actor=actor, idempotency_key=f"f4:{c.key}"
            )
            if isinstance(c, DutyInput):
                cmd = CreateDutyCommand(
                    **common,
                    duty_code=c.duty_code,
                    display_name=c.display_name,
                    description=None,
                    effective_from=c.effective_from,
                )
            elif isinstance(c, ScopeInput):
                scope = c.scope(self.school_id)
                if c.family == "f4":
                    from app.platform.school_facts.f4_policy import F4_SCOPE_SCHEMA

                    schema = F4_SCOPE_SCHEMA
                else:
                    schema = "exact_fact_set" if c.family == "f2" else "exact_family_fact_set"
                cmd = CreateScopeCommand(
                    **common,
                    scope_key=c.key,
                    schema_owner=f"school_facts.{c.family}",
                    schema_name=schema,
                    schema_version=1,
                    reference_document=scope.document,
                    reference_sha256=scope.document_sha256,
                    effective_from=c.effective_from,
                )
            elif isinstance(c, GrantInput):
                cmd = CreateGrantCommand(
                    **common,
                    assignment_id=c.assignment_id,
                    duty_id=c.duty_id,
                    duty_version=c.duty_version,
                    scope_id=c.scope_id,
                    scope_version=c.scope_version,
                    basis_authority="f4_engineering_policy",
                    basis_reference=self.approval_reference,
                    valid_from=c.valid_from,
                    effective_from=c.effective_from,
                    planned_valid_to=c.planned_valid_to,
                )
            else:
                cmd = RevokeGrantCommand(
                    **common,
                    grant_id=c.grant_id,
                    expected_version=c.expected_version,
                    effective_at=c.effective_from,
                )
            result.append(cmd)
        return tuple(result)

    def approval(self) -> F4EngineeringApproval:
        return F4EngineeringApproval(
            self.school_id,
            self.actor_reference,
            self.approval_reference,
            self.valid_from,
            self.valid_to,
            self.grant_epoch,
            self.grant_valid_to,
            frozenset(f4_governance_command_digest(c) for c in self.materialize()),
            self.approved_duty_codes,
            self.approved_assignment_ids,
            tuple({c.proof() for c in self.commands if isinstance(c, GrantInput)}),
        )


class EngineeringPrivateConfiguration(_PrivateConfiguration):
    approval_reference: str = Field(min_length=1, max_length=200)
    grant_epoch: AwareDatetime
    expected_schema_contract: Literal[
        "school-facts-f4-v28", "school-facts-f4-v29", "school-facts-f4-v30", "school-facts-f4-v31"
    ]


def load_manifest(path: Path, expected_digest: str) -> EngineeringManifest:
    if path.stat().st_size > 1_048_576:
        raise ValueError("Manifest is too large")
    data = path.read_bytes()
    if not hmac.compare_digest(sha256(data).hexdigest(), expected_digest):
        raise ValueError("Approved manifest bytes changed")
    return EngineeringManifest.model_validate_json(data)


def command_marker(digest: str, command: GovernanceCommand) -> str:
    return f"CONFIRM-F4:{digest}:{f4_governance_command_digest(command)}"


def review(manifest: EngineeringManifest, digest: str) -> dict[str, object]:
    import json

    return {
        "manifest_sha256": digest,
        "manifest": manifest.model_dump(mode="json"),
        "f2_field_scope_note": "F2 fields are declared projections; grants retain object scope",
        "commands": [
            {
                "index": i,
                "payload": json.loads(f4_governance_command_payload(c)),
                "confirm_marker": command_marker(digest, c),
            }
            for i, c in enumerate(manifest.materialize())
        ],
    }


def build_duties_and_scopes(
    *,
    school_id: UUID,
    actor_reference: str,
    approval_reference: str,
    valid_from: datetime,
    valid_to: datetime,
    grant_epoch: datetime,
    approved_assignment_ids: frozenset[UUID],
    rows: tuple[ScopeMatrixRow, ...],
    existing_duties: tuple[ExistingDutyReference, ...] = (),
) -> EngineeringManifest:
    commands: list[DutyInput | ScopeInput] = []
    duties: set[str] = set()
    for row in rows:
        row.scope(school_id)
        if row.duty_code not in duties:
            duties.add(row.duty_code)
            if row.duty_code not in {d.duty_code for d in existing_duties}:
                commands.append(
                    DutyInput(
                        command="duty",
                        key=row.duty_code,
                        duty_code=row.duty_code,
                        display_name=f"F4 engineering {row.duty_code}",
                        effective_from=valid_from,
                    )
                )
        commands.append(
            ScopeInput(
                **row.model_dump(exclude={"command", "effective_from"}),
                command="scope",
                effective_from=valid_from,
            )
        )
    return EngineeringManifest(
        manifest_version="f4_governance_v1",
        school_id=school_id,
        actor_reference=actor_reference,
        approval_reference=approval_reference,
        valid_from=valid_from,
        valid_to=valid_to,
        grant_epoch=grant_epoch,
        grant_valid_to=grant_epoch + timedelta(days=7),
        approved_duty_codes=frozenset(duties),
        approved_assignment_ids=approved_assignment_ids,
        commands=tuple(commands),
        existing_duties=existing_duties,
    )


def build_grants(
    manifest: EngineeringManifest,
    bindings: list[GrantBinding],
    *,
    grant_valid_from: datetime,
    execution_valid_to: datetime,
    service: Any,
    credential: str,
) -> EngineeringManifest:
    if not bindings or any(
        b.assignment_id not in manifest.approved_assignment_ids for b in bindings
    ):
        raise ValueError("Bindings may not add assignments to the original approval")
    commands = dict(zip((c.key for c in manifest.commands), manifest.materialize(), strict=True))
    inputs = {c.key: c for c in manifest.commands}
    grants: list[GrantInput] = []
    existing = {d.duty_code: d for d in manifest.existing_duties}
    for b in bindings:
        duty, scope = commands.get(b.duty_key), commands.get(b.scope_key)
        scope_input = inputs.get(b.scope_key)
        prior_duty = existing.get(b.duty_key)
        duty_code = (
            duty.duty_code
            if isinstance(duty, CreateDutyCommand)
            else prior_duty.duty_code
            if prior_duty
            else None
        )
        if (
            (not isinstance(duty, CreateDutyCommand) and prior_duty is None)
            or not isinstance(scope, CreateScopeCommand)
            or not isinstance(scope_input, ScopeInput)
            or duty_code != scope_input.duty_code
        ):
            raise ValueError("Exact approved matching duty and scope required")
        duty_receipt = service.receipt(duty, credential=credential) if duty is not None else None
        scope_receipt = service.receipt(scope, credential=credential)
        if (duty_receipt is None and prior_duty is None) or scope_receipt is None:
            raise ValueError("Committed authoritative prerequisite receipts required")
        if duty_receipt is not None:
            duty_id, duty_version = duty_receipt.target_id, duty_receipt.target_version
        elif prior_duty is not None:
            duty_id, duty_version = prior_duty.duty_id, prior_duty.duty_version
        else:
            raise ValueError("Current duty reference is missing")
        if duty_code is None:
            raise ValueError("Current duty code is missing")
        grants.append(
            GrantInput(
                command="grant",
                key=b.key,
                assignment_id=b.assignment_id,
                duty_id=duty_id,
                duty_version=duty_version,
                scope_id=scope_receipt.target_id,
                scope_version=scope_receipt.target_version,
                duty_code=duty_code,
                scope_owner=cast(
                    Literal["school_facts.f2", "school_facts.f3", "school_facts.f4"],
                    scope.schema_owner,
                ),
                scope_sha256=scope.reference_sha256,
                valid_from=grant_valid_from,
                effective_from=grant_valid_from,
                planned_valid_to=manifest.grant_valid_to,
            )
        )
    return EngineeringManifest.model_validate(
        {
            **manifest.model_dump(),
            "valid_from": grant_valid_from,
            "valid_to": execution_valid_to,
            "approved_duty_codes": frozenset(c.duty_code for c in grants),
            "commands": tuple(grants),
            "existing_duties": tuple(
                d for d in manifest.existing_duties if d.duty_code in {c.duty_code for c in grants}
            ),
        }
    )


def grant_reference_matches(
    proof: GrantReferenceProof,
    *,
    school_id: UUID,
    actual_duty_code: str,
    actual_owner: str,
    actual_document: dict[str, object],
    actual_sha256: str,
) -> bool:
    from app.composition.school_fact_f4_engineering_governance import parse_scope

    try:
        scope = parse_scope(actual_owner, actual_document)
        family = actual_owner.rsplit(".", 1)[1]
        allowed = {f"{family}_{scope.fact_kind}_reader"}
        if family == "f4":
            allowed.add(f"f4_{scope.fact_kind}_maintainer")
        elif dependency_maintenance_allowed(
            family, scope.fact_kind, scope.object_keys, scope.creation_keys
        ):
            allowed.add(f"{family}_{scope.fact_kind}_maintainer")
        return (
            actual_duty_code == proof.duty_code
            and actual_duty_code in allowed
            and actual_owner == proof.scope_owner
            and scope.school_id == school_id
            and actual_sha256 == scope.document_sha256 == proof.scope_sha256
            and (
                family == "f4"
                or not scope.creation_keys
                or (
                    actual_duty_code.endswith("_maintainer")
                    and dependency_maintenance_allowed(
                        family, scope.fact_kind, scope.object_keys, scope.creation_keys
                    )
                )
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


def compose(
    config: EngineeringPrivateConfiguration,
    manifest: EngineeringManifest,
    *,
    approval_is_current: Callable[[], bool],
    receipt_only: bool = False,
) -> tuple[F4EngineeringGovernanceService, Any]:
    """Use separately bound read-only dependencies and the existing single Governance Writer."""
    from datetime import UTC

    from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
    from sqlalchemy import create_engine, select, text
    from sqlalchemy.engine import URL
    from sqlalchemy.orm import sessionmaker

    from app.composition.school_fact_directory_services import F2ScopeOwnerLookup
    from app.composition.school_fact_family_authority import F3ScopeOwnerLookup
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.composition.school_fact_reader import (
        SchoolFactReaderSettings,
        build_school_fact_reader_engine,
        verify_school_fact_reader_database,
    )
    from app.platform.governance.application import GovernanceWriter
    from app.platform.governance.models import (
        GovernanceDuty,
        GovernanceDutyVersion,
        GovernanceScopeVersion,
    )
    from app.platform.school_facts.f2_policy import F2ScopeResolver
    from app.platform.school_facts.f3_policy import F3ScopeResolver
    from app.platform.school_facts.f4_policy import F4FactScope, F4ScopeResolver

    class ExplicitReader(SchoolFactReaderSettings):
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

    if (
        manifest.school_id,
        manifest.actor_reference,
        manifest.approval_reference,
        manifest.grant_epoch,
    ) != (
        config.expected_school_id,
        config.actor_reference,
        config.approval_reference,
        config.grant_epoch,
    ):
        raise ValueError("Independent approval identity or original seven-day epoch mismatch")
    if any(
        isinstance(c, GrantInput) and c.assignment_id == config.operator_assignment_id
        for c in manifest.commands
    ):
        raise ValueError("Operator may not grant itself product authority")
    reader = ExplicitReader.model_validate(config.reader)
    if reader.schema_contract != config.expected_schema_contract or (
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
        raise ValueError("Independent Reader must bind the exact F4 database and schema")
    reader_engine = build_school_fact_reader_engine(reader)
    writer_engine = None
    try:
        verify_school_fact_reader_database(reader_engine, settings=reader)
        # The existing Reader has no receipt-table privilege. Recovery uses the
        # supervisor's separately approved short technical window, never new grants.
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
        with writer_engine.connect() as connection:
            role = connection.execute(
                text(
                    "SELECT current_user,session_user,rolsuper,rolbypassrls "
                    "FROM pg_roles WHERE rolname=current_user"
                )
            ).one()
            if role != (config.database_user, config.database_user, False, False):
                raise ValueError("Isolated non-super Governance role required")
            connection.execute(
                text("SELECT app.assert_school_database_anchor(:anchor,:school,:system)"),
                {
                    "anchor": config.expected_anchor_id,
                    "school": config.expected_school_id,
                    "system": config.expected_system_identifier,
                },
            ).scalar_one()
        sessions = sessionmaker(reader_engine, expire_on_commit=False)

        def clock() -> datetime:
            return datetime.now(UTC)

        def f4_scope_exists(scope: F4FactScope, as_of: datetime) -> bool:
            with sessions.begin() as session:
                return (
                    session.scalar(
                        text(
                            "SELECT app.school_fact_f4_scope_is_current"
                            "(:school,:kind,:objects,:as_of)"
                        ),
                        {
                            "school": scope.school_id,
                            "kind": scope.fact_kind,
                            "objects": list(scope.object_keys),
                            "as_of": as_of,
                        },
                    )
                    is True
                )

        def reference_current(proof: GrantReferenceProof) -> bool:
            with sessions.begin() as session:
                duty = session.scalar(
                    select(GovernanceDuty.duty_code)
                    .join(
                        GovernanceDutyVersion,
                        (GovernanceDutyVersion.school_id == GovernanceDuty.school_id)
                        & (GovernanceDutyVersion.duty_id == GovernanceDuty.id),
                    )
                    .where(
                        GovernanceDuty.school_id == manifest.school_id,
                        GovernanceDuty.id == proof.duty_id,
                        GovernanceDutyVersion.version == proof.duty_version,
                        GovernanceDutyVersion.is_active.is_(True),
                        GovernanceDutyVersion.effective_from <= clock(),
                    )
                )
                scope = session.scalar(
                    select(GovernanceScopeVersion).where(
                        GovernanceScopeVersion.school_id == manifest.school_id,
                        GovernanceScopeVersion.scope_id == proof.scope_id,
                        GovernanceScopeVersion.version == proof.scope_version,
                        GovernanceScopeVersion.is_active.is_(True),
                        GovernanceScopeVersion.effective_from <= clock(),
                    )
                )
                return (
                    duty is not None
                    and scope is not None
                    and grant_reference_matches(
                        proof,
                        school_id=manifest.school_id,
                        actual_duty_code=duty,
                        actual_owner=scope.schema_owner,
                        actual_document=scope.reference_document,
                        actual_sha256=scope.reference_sha256,
                    )
                )

        approval = manifest.approval()

        def execution_is_current() -> bool:
            return not receipt_only and approval_is_current()

        authorizer = F4EngineeringCommandAuthorizer(
            approval=approval,
            clock=clock,
            approval_is_current=execution_is_current,
            grant_reference_is_current=reference_current,
        )
        writer = GovernanceWriter(
            session_factory=sessionmaker(writer_engine, expire_on_commit=False),
            authorizer=authorizer,
            transaction_authorizer=authorizer,
            clock=clock,
            dependency_port=ReadOnlyGovernanceDependencies(sessions),
            scope_owner_resolvers={
                "school_facts.f2": F2ScopeResolver(lookup=F2ScopeOwnerLookup(sessions)),
                "school_facts.f3": F3ScopeResolver(
                    lookup_scope=F3ScopeOwnerLookup(sessions).scope_exists
                ),
                "school_facts.f4": F4ScopeResolver(lookup_scope=f4_scope_exists),
            },
        )
        return F4EngineeringGovernanceService(
            writer=writer,
            approval=approval,
            credential=config.credential.get_secret_value(),
            clock=clock,
            approval_is_current=execution_is_current,
        ), (writer_engine, reader_engine)
    except BaseException:
        if writer_engine is not None:
            writer_engine.dispose()
        reader_engine.dispose()
        raise
