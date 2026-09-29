"""F1 one-off deployment CLI. No command is called on import; no ordinary HTTP bootstrap."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, cast
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator
from sqlalchemy import Engine, create_engine, select
from sqlalchemy import event as event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.composition.school_fact_initialization import SchoolFactInitializer
from app.core.settings import Settings
from app.db.connection import build_engine, build_session_factory, probe_database
from app.db.migration_settings import MigrationSettings
from app.platform.school_facts.initialization_contracts import (
    INITIALIZATION_AUTHORITY,
    INITIALIZATION_HEAD,
    SchoolFactInitializationRequest,
)

DATABASE = "campus_ai_school_facts_f1"
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class RootManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    db: Literal["campus_ai_school_facts_f1"]
    system_identifier: Annotated[str, StringConstraints(pattern=r"^[0-9]{10,32}$")]
    school_id: UUID
    source_key: Text
    school_code: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    display_name: Text
    timezone_name: Literal["Asia/Shanghai"]

    @model_validator(mode="after")
    def valid_timezone(self):
        ZoneInfo(self.timezone_name)
        return self


class _InitOnly:
    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return (init_settings,)


class ExplicitMigrationSettings(_InitOnly, MigrationSettings):
    pass


class ExplicitRuntimeSettings(_InitOnly, Settings):
    pass


def read_json(path: Path):
    if path.stat().st_size > 1_048_576:
        raise ValueError("private input is too large")
    return json.loads(path.read_text(encoding="utf-8"))


def write_private(path: Path, value):
    # Never print credentials/manifest; new file only, mode 0600 on POSIX.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")


def require_target(settings):
    if settings.db != DATABASE or settings.user != "campus_ai_migration":
        raise ValueError("F1 migration database identity rejected")
    if (settings.host, settings.port) not in {("postgres", 5432), ("127.0.0.1", 55433)}:
        raise ValueError("F1 database endpoint rejected")


def root(engine, manifest: RootManifest, *, inspect_only: bool = False):
    from app.composition.school_fact_deployment_root import controlled_school_root

    return controlled_school_root(
        engine, manifest, expected_database=DATABASE, inspect_only=inspect_only
    )


def migration_session_factory(engine: Engine) -> sessionmaker[Session]:
    from app.composition.school_fact_deployment_root import migration_session_factory as checked

    return checked(engine)


def runtime_factory(config):
    settings = ExplicitRuntimeSettings.model_validate(config["runtime"])
    if (
        settings.app_env not in {"development", "test"}
        or settings.postgres_schema_contract != "school-facts-f1-v22"
        or settings.postgres_db != DATABASE
        or settings.postgres_user != "campus_ai_runtime"
        or settings.postgres_expected_school_id is None
    ):
        raise ValueError("F1 runtime configuration rejected")
    engine = build_engine(settings)
    try:
        probe_database(engine)
    except BaseException:
        engine.dispose()
        raise
    return settings, engine, build_session_factory(engine)


def initialize_identity(config):
    from app.composition.school_fact_product_identity import ENGINEERING_ACCOUNTS
    from app.platform.identity.service import find_or_create_namespaced_test_user

    _settings, engine, sessions = runtime_factory(config)
    try:
        accounts = []
        for account in ENGINEERING_ACCOUNTS:
            with sessions() as session:
                user = find_or_create_namespaced_test_user(
                    session, account.external_subject, account.label
                )
                if not user.is_active:
                    raise ValueError("existing engineering account is inactive")
                accounts.append({"ref": account.ref, "user_id": user.id})
        return {"accounts": accounts}
    finally:
        engine.dispose()


def configure_governance(sessions, request, initializer):
    from app.composition.governance_dependencies import SqlAlchemyGovernanceDependencyAdapter
    from app.platform.governance.bootstrap import build_migration_bootstrap_writer
    from app.platform.governance.contracts import (
        CreateDutyCommand,
        CreateGrantCommand,
        CreateScopeCommand,
        GovernanceActorContext,
        OpenGovernanceBootstrapCommand,
    )
    from app.platform.school_facts.read_policy import (
        OFFICIAL_CONTACT_MAINTAINER_DUTY,
        OFFICIAL_CONTACT_READER_DUTY,
        SCHOOL_SCOPE_OWNER,
        SCHOOL_SCOPE_SCHEMA,
        SingleSchoolScopeResolver,
        school_scope_document_hash,
    )

    receipt = initializer.result(request)
    if receipt is None or receipt["state"] != "applied":
        raise ValueError("confirmed first maintainer facts are required")
    assignment = UUID(
        next(
            item["target_id"]
            for item in receipt["result"]["targets"]
            if item["fact_kind"] == "assignment"
        )
    )
    effective = min(change.effective_at for change in request.change_set.changes)

    def actor(name):
        return GovernanceActorContext(
            school_id=request.school_id,
            actor_kind="platform_bootstrap",
            actor_reference=INITIALIZATION_AUTHORITY,
            actor_assignment_id=None,
            actor_assignment_version=None,
            request_id=uuid5(request.request_id, name),
            reason=request.approval_reference,
        )

    writer = build_migration_bootstrap_writer(
        session_factory=sessions,
        clock=lambda: datetime.now(UTC),
        dependency_port=SqlAlchemyGovernanceDependencyAdapter(sessions),
        scope_owner_resolvers={SCHOOL_SCOPE_OWNER: SingleSchoolScopeResolver()},
    )
    writer.open_governance_bootstrap(
        OpenGovernanceBootstrapCommand(
            school_id=request.school_id,
            authority_reference=INITIALIZATION_AUTHORITY,
            actor=actor("open"),
            idempotency_key=f"f1:{request.request_id}:open",
        )
    )
    scope = writer.create_scope(
        CreateScopeCommand(
            school_id=request.school_id,
            scope_key="f1:official-contacts:single-school",
            schema_owner=SCHOOL_SCOPE_OWNER,
            schema_name=SCHOOL_SCOPE_SCHEMA,
            schema_version=1,
            reference_document={"school_id": str(request.school_id)},
            reference_sha256=school_scope_document_hash(request.school_id),
            effective_from=effective,
            actor=actor("scope"),
            idempotency_key=f"f1:{request.request_id}:scope",
        )
    )
    for code, label in (
        (OFFICIAL_CONTACT_READER_DUTY, "官方联系方式查询"),
        (OFFICIAL_CONTACT_MAINTAINER_DUTY, "官方联系方式维护"),
    ):
        duty = writer.create_duty(
            CreateDutyCommand(
                school_id=request.school_id,
                duty_code=code,
                display_name=label,
                description="F1首链明确批准的有限职责",
                effective_from=effective,
                actor=actor(code),
                idempotency_key=f"f1:{request.request_id}:{code}",
            )
        )
        writer.create_grant(
            CreateGrantCommand(
                school_id=request.school_id,
                assignment_id=assignment,
                duty_id=duty.target_id,
                duty_version=duty.target_version,
                scope_id=scope.target_id,
                scope_version=scope.target_version,
                basis_authority="deployment.approval",
                basis_reference=request.approval_reference,
                valid_from=effective,
                effective_from=effective,
                planned_valid_to=None,
                actor=actor(f"grant:{code}"),
                idempotency_key=f"f1:{request.request_id}:grant:{code}",
            )
        )
    return {"scope_id": scope.target_id, "assignment_id": assignment}


def close_initialization(config, sessions, request, initializer, scope_id):
    from app.composition.governance_dependencies import SqlAlchemyGovernanceDependencyAdapter
    from app.composition.school_fact_initialization_readiness import FirstMaintainerReadiness
    from app.composition.school_fact_reader import SchoolFactReaderSettings
    from app.platform.governance.bootstrap import build_migration_bootstrap_writer
    from app.platform.governance.contracts import (
        CloseGovernanceBootstrapCommand,
        GovernanceActorContext,
    )
    from app.platform.governance.models import GovernanceBootstrapState
    from app.platform.school_facts.read_policy import SCHOOL_SCOPE_OWNER, SingleSchoolScopeResolver

    class ReaderSettings(_InitOnly, SchoolFactReaderSettings):
        pass

    readiness = FirstMaintainerReadiness(
        runtime_settings=ExplicitRuntimeSettings.model_validate(config["runtime"]),
        reader_settings=ReaderSettings.model_validate(config["reader"]),
        scope_id=scope_id,
    )
    receipt = initializer.result(request)
    if receipt is None or receipt["result"] is None:
        raise ValueError("confirmed initial facts are required")
    # All facts and duties must work before permanently closing the external Governance authority.
    from app.composition.school_fact_initial_publication import verify_initial_publication_closed

    with sessions() as session:
        verify_initial_publication_closed(
            session,
            request=request,
            initialization_id=UUID(receipt["initialization_id"]),
        )
    readiness.verify_before_governance_close(request, receipt["result"])
    writer = build_migration_bootstrap_writer(
        session_factory=sessions,
        clock=lambda: datetime.now(UTC),
        dependency_port=SqlAlchemyGovernanceDependencyAdapter(sessions),
        scope_owner_resolvers={SCHOOL_SCOPE_OWNER: SingleSchoolScopeResolver()},
    )
    with sessions() as session:
        state = session.scalar(
            select(GovernanceBootstrapState).where(
                GovernanceBootstrapState.school_id == request.school_id
            )
        )
        if state is None:
            raise ValueError("Governance bootstrap is missing")
        version = state.version if state.state == "open" else state.version - 1
    writer.close_governance_bootstrap(
        CloseGovernanceBootstrapCommand(
            school_id=request.school_id,
            authority_reference=INITIALIZATION_AUTHORITY,
            expected_version=version,
            actor=GovernanceActorContext(
                school_id=request.school_id,
                actor_kind="platform_bootstrap",
                actor_reference=INITIALIZATION_AUTHORITY,
                actor_assignment_id=None,
                actor_assignment_version=None,
                request_id=uuid5(request.request_id, "close"),
                reason=request.approval_reference,
            ),
            idempotency_key=f"f1:{request.request_id}:close",
        )
    )
    initializer.close(request, readiness=readiness)
    return {"state": "closed", "scope_id": scope_id}


def require_publication_session(arguments, manifest_sha256):
    if arguments.session_file is None:
        raise ValueError("an existing private publication session receipt is required")
    receipt = read_json(arguments.session_file)
    token = receipt.get("identity_session_token")
    if receipt.get("manifest_sha256") != manifest_sha256 or not isinstance(token, str) or not token:
        raise ValueError("publication session does not match the approved manifest")
    return token


def publication_operation(config, arguments):
    from redis import Redis

    from app.composition.governance_dependencies import SqlAlchemyGovernanceDependencyAdapter
    from app.composition.school_fact_initial_publication import (
        PUBLICATION_MODULE,
        InitialPublicationFinalGuard,
        InitialPublicationManifest,
        InitialPublicationPolicy,
        InitialPublicationScopeResolver,
        InitialPublicationService,
        PublicationGrantReceipt,
        configure_initial_publication_grant,
        end_initial_publication_grant,
    )
    from app.composition.school_fact_product_identity import (
        build_school_fact_product_identity_service,
    )
    from app.composition.school_fact_product_services import (
        ProductThinAuthorizationService,
        ReadOnlyGovernanceDependencies,
    )
    from app.composition.school_fact_product_writer import (
        ExplicitReaderSettings,
        ExplicitWriterSettings,
    )
    from app.composition.school_fact_reader import build_school_fact_reader_engine
    from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
    from app.composition.school_fact_writer_settings import build_school_fact_writer_engine
    from app.platform.governance.basis import GovernanceAuthorizationBasisService
    from app.platform.governance.bootstrap import build_migration_bootstrap_writer
    from app.platform.governance.reader import GovernanceAuthorityReader
    from app.platform.identity.context_proof import ContextProofSigner
    from app.platform.master_data_imports.models import SchoolFactInitialization
    from app.platform.school_facts.contracts import ConfirmationReceiptV1

    if arguments.input is None:
        raise ValueError("approved publication manifest is required")
    manifest = InitialPublicationManifest.model_validate_json(
        arguments.input.read_text(encoding="utf-8")
    )
    migration = ExplicitMigrationSettings.model_validate(config["migration"])
    require_target(migration)
    migration_engine = create_engine(
        migration.database_url, poolclass=NullPool, hide_parameters=True
    )
    runtime = reader = writer_engine = redis = None
    try:
        sessions = migration_session_factory(migration_engine)
        with sessions() as session:
            stored = session.scalar(
                select(SchoolFactInitialization).where(
                    SchoolFactInitialization.id == manifest.initialization_id,
                    SchoolFactInitialization.school_id == manifest.school_id,
                )
            )
            allowed_states = (
                {"applied", "closed"} if arguments.action == "publication-result" else {"applied"}
            )
            if stored is None or stored.state not in allowed_states or not stored.result_payload:
                raise ValueError("first maintainer initialization must be applied and open")
            targets = cast(list[dict[str, object]], stored.result_payload["targets"])
            assignment = next(
                item["target_id"] for item in targets if item["fact_kind"] == "assignment"
            )
            if str(manifest.assignment_id) != assignment:
                raise ValueError("publication does not use the actual first assignment")
        if arguments.action == "publication-recover":
            from app.composition.school_fact_initial_publication_recovery import (
                InitialPublicationRecoveryRequest,
                recover_initial_publication,
            )

            if arguments.recovery_request is None:
                raise ValueError("a fixed private recovery request is required")
            recovery = InitialPublicationRecoveryRequest.model_validate_json(
                arguments.recovery_request.read_text(encoding="utf-8")
            )
            return asdict(
                recover_initial_publication(
                    session_factory=sessions,
                    manifest=manifest,
                    request=recovery,
                )
            )
        if arguments.action == "publication-grant":
            gov_writer = build_migration_bootstrap_writer(
                session_factory=sessions,
                clock=lambda: datetime.now(UTC),
                dependency_port=SqlAlchemyGovernanceDependencyAdapter(sessions),
                scope_owner_resolvers={PUBLICATION_MODULE: InitialPublicationScopeResolver()},
            )
            return asdict(configure_initial_publication_grant(gov_writer, manifest=manifest))
        if arguments.grant_receipt is None:
            raise ValueError("fixed publication Grant receipt required")
        saved_grant = read_json(arguments.grant_receipt)
        if "grant" in saved_grant:
            saved_grant = saved_grant["grant"]
        if not isinstance(saved_grant, dict):
            raise ValueError("recovery has already published; use closure instead")
        grant = PublicationGrantReceipt(
            scope_id=UUID(saved_grant["scope_id"]),
            grant_id=UUID(saved_grant["grant_id"]),
            grant_version=saved_grant["grant_version"],
        )
        if arguments.action == "publication-end":
            return asdict(
                end_initial_publication_grant(
                    session_factory=sessions,
                    manifest=manifest,
                    grant=grant,
                    dependency_port=SqlAlchemyGovernanceDependencyAdapter(sessions),
                )
            )
        reader_settings = ExplicitReaderSettings.model_validate(config["reader"])
        writer_settings = ExplicitWriterSettings.model_validate(config["writer"])
        if any(
            settings.schema_contract != "school-facts-f1-v22"
            or settings.expected_school_id != manifest.school_id
            or settings.database_name != DATABASE
            for settings in (reader_settings, writer_settings)
        ):
            raise ValueError("publication component school/schema mismatch")
        settings, runtime, runtime_sessions = runtime_factory(config)
        if settings.postgres_expected_school_id != manifest.school_id:
            raise ValueError("publication identity school mismatch")
        reader = build_school_fact_reader_engine(reader_settings)
        reader_sessions = sessionmaker(bind=reader, expire_on_commit=False)
        thin = ProductThinAuthorizationService(
            governance_basis_service=GovernanceAuthorizationBasisService(
                session_factory=reader_sessions,
                authority_reader=GovernanceAuthorityReader(
                    session_factory=reader_sessions,
                    dependency_port=ReadOnlyGovernanceDependencies(reader_sessions),
                    scope_owner_resolvers={PUBLICATION_MODULE: InitialPublicationScopeResolver()},
                ),
            ),
            module_policies={PUBLICATION_MODULE: InitialPublicationPolicy(manifest)},
        )
        guard = InitialPublicationFinalGuard(
            manifest=manifest, scope_id=grant.scope_id, grant_id=grant.grant_id, thin_service=thin
        )
        redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        identity = build_school_fact_product_identity_service(
            app_env=settings.app_env,
            session_factory=runtime_sessions,
            redis=redis,
            proof_signer=ContextProofSigner(settings.rls_context_signing_key),
            can_maintain=lambda actor: guard.require_current(actor).allowed,
        )
        if arguments.action == "publication-session":
            token = identity.login("maintainer")
            try:
                view = identity.session_view(token)
                if len(view.contexts) != 1:
                    raise ValueError("first maintainer real context is not unique")
                identity.select_context(token, view.contexts[0].ref)
                current = identity.resolve_request(token)
                guard.require_current(current.actor)
                return {
                    "manifest_sha256": manifest.manifest_sha256,
                    "identity_session_token": token,
                }
            except BaseException:
                identity.logout(token)
                raise
        token = require_publication_session(arguments, manifest.manifest_sha256)
        current = identity.resolve_request(token)
        writer_engine = build_school_fact_writer_engine(writer_settings)
        kernel = SqlAlchemySchoolFactWriterExecutorKernel(
            engine=writer_engine,
            settings=writer_settings,
            final_authorization_guard=guard,
        )
        publication = InitialPublicationService(
            manifest=manifest,
            grant_id=grant.grant_id,
            kernel=kernel,
            require_current=guard.require_current,
        )
        if arguments.action == "publication-prepare":
            receipt = publication.prepare(current)
            return {
                "manifest_sha256": manifest.manifest_sha256,
                "confirmation": receipt.model_dump(mode="json"),
            }
        if arguments.action == "publication-confirm":
            if arguments.credential_file is None:
                raise ValueError("private publication preparation receipt required")
            preparation = read_json(arguments.credential_file)
            if preparation.get("manifest_sha256") != manifest.manifest_sha256:
                raise ValueError("publication preparation changed")
            receipt = ConfirmationReceiptV1.model_validate_json(
                json.dumps(preparation["confirmation"])
            )
            return asdict(publication.confirm(current, receipt))
        return asdict(publication.result(current))
    finally:
        if redis is not None:
            redis.close()
        for engine in (writer_engine, reader, runtime, migration_engine):
            if engine is not None:
                engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "migrate",
            "root",
            "root-result",
            "identity",
            "prepare",
            "confirm",
            "result",
            "governance",
            "close",
            "publication-grant",
            "publication-session",
            "publication-prepare",
            "publication-confirm",
            "publication-result",
            "publication-end",
            "publication-recover",
        ),
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--scope-id", type=UUID)
    parser.add_argument("--session-file", type=Path)
    parser.add_argument("--grant-receipt", type=Path)
    parser.add_argument("--recovery-request", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    engine = None
    try:
        if arguments.output.exists():
            raise ValueError("output must be a new private receipt")
        config = read_json(arguments.config)
        if config.get("purpose") != "school-facts-f1-synthetic":
            raise ValueError("F1 purpose rejected")
        if arguments.action == "identity":
            result = initialize_identity(config)
        elif arguments.action.startswith("publication-"):
            result = publication_operation(config, arguments)
        else:
            migration = ExplicitMigrationSettings.model_validate(config["migration"])
            require_target(migration)
            if arguments.action == "migrate":
                env = {
                    key: os.environ[key]
                    for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
                    if key in os.environ
                }
                for field in ("host", "port", "db", "user", "password", "context_signing_key"):
                    value = getattr(migration, field)
                    env[f"POSTGRES_MIGRATION_{field.upper()}"] = str(
                        value.get_secret_value() if hasattr(value, "get_secret_value") else value
                    )
                result_code = subprocess.run(
                    [sys.executable, "-m", "alembic", "upgrade", INITIALIZATION_HEAD],
                    cwd=Path(__file__).resolve().parents[1] / "apps/api",
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                ).returncode
                if result_code:
                    raise ValueError("migration failed; inspect privately")
                result = {"schema": INITIALIZATION_HEAD}
            else:
                if arguments.input is None:
                    raise ValueError("approved input required")
                engine = create_engine(
                    migration.database_url, poolclass=NullPool, hide_parameters=True
                )
                if arguments.action in {"root", "root-result"}:
                    result = root(
                        engine,
                        RootManifest.model_validate(read_json(arguments.input)),
                        inspect_only=arguments.action == "root-result",
                    )
                else:
                    request = SchoolFactInitializationRequest.model_validate_json(
                        arguments.input.read_text(encoding="utf-8")
                    )
                    sessions = migration_session_factory(engine)
                    initializer = SchoolFactInitializer(session_factory=sessions)
                    if arguments.action == "prepare":
                        result = asdict(initializer.prepare(request))
                    elif arguments.action == "confirm":
                        if arguments.credential_file is None:
                            raise ValueError("private preparation receipt required")
                        result = initializer.confirm(
                            request, credential=read_json(arguments.credential_file)["credential"]
                        )
                    elif arguments.action == "result":
                        result = initializer.result(request)
                    elif arguments.action == "governance":
                        result = configure_governance(sessions, request, initializer)
                    else:
                        if arguments.scope_id is None:
                            raise ValueError("verified scope receipt required")
                        result = close_initialization(
                            config, sessions, request, initializer, arguments.scope_id
                        )
        write_private(arguments.output, result)
        print("F1 one-off acknowledged; private receipt written.")
        return 0
    except Exception:
        print(
            "F1 operation rejected or outcome unknown; use result with the same approved input; "
            "inspect privately before any retry.",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
