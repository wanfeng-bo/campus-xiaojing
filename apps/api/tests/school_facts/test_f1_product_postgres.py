"""Real F1 PG16/Redis gate; collection alone is not execution or process-isolation evidence."""

from __future__ import annotations

import json
import os
import runpy
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Barrier
from typing import LiteralString, cast
from uuid import UUID, uuid4

import pytest
from psycopg import Error, connect
from psycopg.errors import InsufficientPrivilege
from pydantic import SecretStr
from redis import Redis
from sqlalchemy import URL, create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.composition.governance_dependencies import SqlAlchemyGovernanceDependencyAdapter
from app.composition.school_fact_initial_publication import (
    PUBLICATION_MODULE,
    InitialPublicationFinalGuard,
    InitialPublicationManifest,
    InitialPublicationPolicy,
    InitialPublicationScopeResolver,
    InitialPublicationService,
    configure_initial_publication_grant,
    end_initial_publication_grant,
    inspect_initial_publication,
)
from app.composition.school_fact_initial_publication_recovery import (
    InitialPublicationRecoveryRequest,
    recover_initial_publication,
)
from app.composition.school_fact_initialization import SchoolFactInitializer
from app.composition.school_fact_product import resolve_contact_target
from app.composition.school_fact_product_identity import build_school_fact_product_identity_service
from app.composition.school_fact_product_services import (
    ProductThinAuthorizationService,
    ReadOnlyGovernanceDependencies,
    create_product_thin_service,
)
from app.composition.school_fact_product_settings import (
    ExplicitReaderSettings,
    ExplicitRuntimeSettings,
)
from app.composition.school_fact_product_writer import ExplicitWriterSettings
from app.composition.school_fact_query_repository import (
    OfficialContactFinalAuthorizationGuard,
    OfficialContactQueryRepository,
)
from app.composition.school_fact_reader import (
    build_school_fact_reader_engine,
    verify_school_fact_reader_database,
)
from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from app.composition.school_fact_writer_settings import (
    build_school_fact_writer_engine,
    verify_school_fact_writer_database,
)
from app.db.connection import (
    build_engine,
    build_session_factory,
    verify_runtime_context_signing_key,
)
from app.platform.authorization.secure_reader_adapters import (
    SqlAlchemyRepeatableReadExecutor,
    ThinAuthorizationReadAdapter,
)
from app.platform.authorization.secure_reader_compiler import ServerOwnedProjectionCompiler
from app.platform.authorization.secure_reader_registry import official_contact_operation_registry
from app.platform.authorization.secure_reader_service import SecureCursorCodec, SecureReader
from app.platform.governance.application import GovernanceWriter
from app.platform.governance.basis import GovernanceAuthorizationBasisService
from app.platform.governance.bootstrap import build_migration_bootstrap_writer
from app.platform.governance.contracts import GovernanceAuthorityQuery
from app.platform.governance.reader import GovernanceAuthorityReader
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import compose_command_context
from app.platform.identity.service import find_or_create_namespaced_test_user
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    FactOriginV1,
)
from app.platform.school_facts.initialization_contracts import (
    SchoolFactInitializationRejected,
    SchoolFactInitializationRequest,
)
from app.platform.school_facts.ports import SchoolFactChangeProvenance
from app.platform.school_facts.read_policy import (
    OFFICIAL_CONTACT_READER_DUTY,
    SCHOOL_SCOPE_OWNER,
    SingleSchoolScopeResolver,
)
from tests.master_data.conftest import _TEST_CONTEXT_SIGNING_KEY, PostgresRoles

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _migration_engine(roles: PostgresRoles):
    params = roles.migration_connection_parameters
    return create_engine(
        URL.create(
            "postgresql+psycopg",
            username=params["user"],
            password=params["password"],
            host=params["host"],
            port=params["port"],
            database=params["dbname"],
        ),
        poolclass=NullPool,
    )


def _root(roles: PostgresRoles, school_id: UUID) -> UUID:
    # Infrastructure root only; revision20's existing trigger creates and binds the anchor.
    with roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "INSERT INTO schools(id,source_key,school_code,display_name,"
            "timezone_name,status,version) "
            "VALUES(%s,'school:f1-engineering','F1-ENGINEERING','合成工程学校','Asia/Shanghai','active',1)",
            (school_id,),
        )
        anchor = connection.execute(
            "SELECT anchor_id,school_id,cluster_system_identifier,database_name "
            "FROM school_database_anchor"
        ).fetchone()
    assert anchor is not None
    assert anchor[1] == school_id
    assert anchor[2] == roles.disposable_cluster_system_identifier
    assert anchor[3] == roles.runtime_connection_parameters["dbname"]
    return anchor[0]


def _request(roles: PostgresRoles, school_id: UUID, anchor_id: UUID):
    document = json.loads(
        (PROJECT_ROOT / "infra/school-facts-f1/identity-manifest.example.json").read_text(
            encoding="utf-8"
        )
    )
    document.update(
        school_id=str(school_id),
        anchor_id=str(anchor_id),
        expected_system_identifier=roles.disposable_cluster_system_identifier,
        request_id=str(uuid4()),
        approval_reference="pytest:explicit-disposable-f1",
        operator_reference="pytest:disposable-cluster-operator",
    )
    document["change_set"]["source_sha256"] = sha256(
        b"pytest:synthetic-first-maintainer"
    ).hexdigest()
    effective = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    for change in document["change_set"]["changes"]:
        change["effective_at"] = effective
        if change["fact_kind"] == "assignment":
            change["content"]["approval_reference"] = document["approval_reference"]
    return SchoolFactInitializationRequest.model_validate_json(json.dumps(document))


def _runtime_settings(roles: PostgresRoles, school_id: UUID, anchor_id: UUID):
    values = roles.runtime_connection_parameters
    return ExplicitRuntimeSettings(
        app_env="test",
        identity_provider="local_test",
        postgres_schema_contract="school-facts-f1-v22",
        postgres_host=values["host"],
        postgres_port=values["port"],
        postgres_db=values["dbname"],
        postgres_user=values["user"],
        postgres_password=values["password"],
        postgres_expected_anchor_id=anchor_id,
        postgres_expected_school_id=school_id,
        postgres_expected_system_identifier=roles.disposable_cluster_system_identifier,
        rls_context_signing_key=SecretStr(_TEST_CONTEXT_SIGNING_KEY),
        redis_host=os.environ["REDIS_HOST"],
        redis_port=int(os.environ["REDIS_PORT"]),
        redis_password=os.environ["REDIS_PASSWORD"],
    )


def _role_settings(roles, school_id, anchor_id, *, writer=False):
    params = roles.writer_connection_parameters if writer else roles.reader_connection_parameters
    common = dict(
        schema_contract="school-facts-f1-v22",
        database_host=params["host"],
        database_port=params["port"],
        database_name=params["dbname"],
        database_password=params["password"],
        expected_anchor_id=anchor_id,
        expected_school_id=school_id,
        expected_system_identifier=roles.disposable_cluster_system_identifier,
        # SecureReader holds a read snapshot while Governance opens a second read session.
        pool_size=1 if writer else 4,
        max_overflow=0,
    )
    if writer:
        return ExplicitWriterSettings.model_validate(
            dict(
                **common,
                confirmation_hmac_key="pytest-f1-confirmation-secret-" * 2,
                caller_auth_key="pytest-f1-caller-secret-" * 2,
                context_transport_hmac_key="pytest-f1-transport-secret-" * 2,
                rls_context_signing_key=_TEST_CONTEXT_SIGNING_KEY,
            )
        )
    return ExplicitReaderSettings.model_validate(
        dict(**common, cursor_hmac_key="pytest-f1-cursor-secret-" * 2)
    )


def _query(reader_sessions, thin, scope_id):
    return OfficialContactQueryRepository(
        reader=SecureReader(
            registry=official_contact_operation_registry(),
            compiler=ServerOwnedProjectionCompiler(),
            authorizer=ThinAuthorizationReadAdapter(
                thin_service=thin, scope_id_resolver=lambda *_: scope_id
            ),
            executor=SqlAlchemyRepeatableReadExecutor(reader_sessions),
            clock=lambda: datetime.now(UTC),
            cursor_codec=SecureCursorCodec(
                b"pytest-f1-cursor-secret-" * 2, key_version=1, ttl_seconds=120
            ),
        )
    )


@pytest.mark.integration
@pytest.mark.parametrize("recovery_mode", ["direct", "expired", "concurrent"])
def test_f1_real_initialization_identity_publication_reader_writer_round_trip(
    school_facts_f1_schema: PostgresRoles,
    recovery_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roles = school_facts_f1_schema
    # No feature-local skip: the inherited integration fixture enforces the disposable PG16 gate.
    # Redis is real as well. This does not prove a separately hosted HTTP Writer process.
    school_id = uuid4()
    anchor_id = _root(roles, school_id)
    request = _request(roles, school_id, anchor_id)
    settings = _runtime_settings(roles, school_id, anchor_id)
    with ExitStack() as resources:
        runtime_engine = build_engine(settings)
        resources.callback(runtime_engine.dispose)
        verify_runtime_context_signing_key(runtime_engine, settings)
        runtime_sessions = build_session_factory(runtime_engine)
        # The F1 account path must also work without the unrelated legacy-claim capability.
        # This narrows only this disposable clone's function ACL, never cluster role membership.
        with roles.admin_connection() as connection:
            connection.execute(
                "REVOKE EXECUTE ON FUNCTION "
                "app.claim_legacy_identity_user_id(text,text,text,text) FROM campus_ai_runtime"
            )
        with runtime_sessions() as session:
            principal = session.execute(
                text(
                    "SELECT session_user,current_user,has_function_privilege("
                    "current_user,'app.claim_legacy_identity_user_id(text,text,text,text)','EXECUTE')"
                )
            ).one()
            assert principal == ("campus_ai_runtime", "campus_ai_runtime", False)
            user = find_or_create_namespaced_test_user(session, "f1-maintainer", "合成工程维护员")
            account_id = user.id
        migration = _migration_engine(roles)
        resources.callback(migration.dispose)
        migration_sessions = sessionmaker(bind=migration, expire_on_commit=False)
        clock = [datetime.now(UTC)]
        if recovery_mode == "expired":
            clock[0] -= timedelta(minutes=11)
        initializer = SchoolFactInitializer(
            session_factory=migration_sessions, clock=lambda: clock[0]
        )
        prepared = initializer.prepare(request)
        assert prepared.credential is not None
        if recovery_mode == "expired":
            old = prepared
            assert old.credential is not None
            clock[0] = datetime.now(UTC)
            with pytest.raises(SchoolFactInitializationRejected, match="expired"):
                initializer.confirm(request, credential=old.credential)
            prepared = initializer.prepare(request)
            assert prepared.initialization_id == old.initialization_id
            assert prepared.credential is not None and prepared.credential != old.credential
            with pytest.raises(SchoolFactInitializationRejected, match="confirmation rejected"):
                initializer.confirm(request, credential=old.credential)
        initialized = initializer.confirm(request, credential=prepared.credential)
        assert initialized["changed_count"] == len(request.change_set.changes)
        assert initializer.confirm(request, credential=prepared.credential) == initialized
        cli = runpy.run_path(str(PROJECT_ROOT / "scripts/school_facts_f1_init.py"))
        migration_sessions = cli["migration_session_factory"](migration)
        governance = cli["configure_governance"](migration_sessions, request, initializer)
        scope_id, assignment_id = governance["scope_id"], governance["assignment_id"]

        reader_settings = _role_settings(roles, school_id, anchor_id)
        assert isinstance(reader_settings, ExplicitReaderSettings)
        reader_engine = build_school_fact_reader_engine(reader_settings)
        resources.callback(reader_engine.dispose)
        assert (
            verify_school_fact_reader_database(reader_engine, settings=reader_settings).alembic_head
            == "20260905_22"
        )
        reader_sessions = sessionmaker(bind=reader_engine, expire_on_commit=False)
        thin = create_product_thin_service(reader_sessions)
        guard = OfficialContactFinalAuthorizationGuard(thin_service=thin, scope_id=scope_id)
        query = _query(reader_sessions, thin, scope_id)
        redis = Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        resources.callback(redis.close)
        assert redis.ping()
        identity = build_school_fact_product_identity_service(
            app_env="test",
            session_factory=runtime_sessions,
            redis=redis,
            proof_signer=ContextProofSigner.from_settings(settings),
            can_maintain=lambda actor: guard.require_current(actor).allowed,
        )
        token = identity.login("maintainer")
        resources.callback(identity.logout, token)
        contexts = identity.session_view(token).contexts
        assert len(contexts) == 1
        identity.select_context(token, contexts[0].ref)
        current = identity.resolve_request(token)
        assert current.actor.account_user_id == account_id
        assert current.actor.assignment_id == assignment_id
        dependency = ReadOnlyGovernanceDependencies(reader_sessions)
        assignment = dependency.read_assignment(
            school_id=school_id, assignment_id=assignment_id, as_of=datetime.now(UTC)
        )
        assert assignment is not None
        assert (
            dependency.read_person_root(
                school_id=school_id, person_id=assignment.person_id, as_of=datetime.now(UTC)
            )
            is not None
        )
        assert (
            dependency.read_organization_root(
                school_id=school_id,
                organization_id=assignment.organization_id,
                as_of=datetime.now(UTC),
            )
            is not None
        )
        authority = GovernanceAuthorityReader(
            session_factory=reader_sessions,
            dependency_port=dependency,
            scope_owner_resolvers={SCHOOL_SCOPE_OWNER: SingleSchoolScopeResolver()},
        )
        authority.resolve(
            GovernanceAuthorityQuery(
                school_id=school_id,
                assignment_id=assignment_id,
                duty_code=OFFICIAL_CONTACT_READER_DUTY,
                scope_id=scope_id,
                as_of=datetime.now(UTC),
            )
        )
        assert (
            query.read_official_contacts(actor=current.actor, campus_name="合成主校区").status
            == "unmatched"
        )

        if recovery_mode == "direct":
            # A missing required capability is source unavailability, never missing school data.
            with roles.admin_connection() as connection:
                connection.execute(
                    "REVOKE EXECUTE ON FUNCTION "
                    "app.school_fact_reader_assignment_terminal(uuid,uuid,bigint) "
                    "FROM campus_ai_school_fact_reader"
                )
            try:
                assert (
                    query.read_official_contacts(
                        actor=current.actor, campus_name="合成主校区"
                    ).status
                    == "source_unavailable"
                )
            finally:
                with roles.admin_connection() as connection:
                    connection.execute(
                        "GRANT EXECUTE ON FUNCTION "
                        "app.school_fact_reader_assignment_terminal(uuid,uuid,bigint) "
                        "TO campus_ai_school_fact_reader"
                    )
            assert (
                query.read_official_contacts(actor=current.actor, campus_name="合成主校区").status
                == "unmatched"
            )
        now = datetime.now(UTC)
        if recovery_mode == "expired":
            now -= timedelta(minutes=11)
        from tests.test_f1_initial_publication import change_set

        publication_change = change_set(base_version=initialized["target_version"])
        publication_change = publication_change.model_copy(
            update={
                "changes": tuple(
                    change.model_copy(update={"effective_at": now})
                    for change in publication_change.changes
                )
            }
        )
        manifest = InitialPublicationManifest(
            school_id=school_id,
            initialization_id=prepared.initialization_id,
            assignment_id=assignment_id,
            request_id=uuid4(),
            approved_at=now,
            expires_at=now + timedelta(minutes=10),
            approval_reference="pytest:synthetic-contact-publication",
            change_set=publication_change,
        )
        dependencies = SqlAlchemyGovernanceDependencyAdapter(migration_sessions)
        governance_writer = build_migration_bootstrap_writer(
            session_factory=migration_sessions,
            clock=lambda: datetime.now(UTC),
            dependency_port=dependencies,
            scope_owner_resolvers={PUBLICATION_MODULE: InitialPublicationScopeResolver()},
        )
        grant = configure_initial_publication_grant(governance_writer, manifest=manifest)
        authority = GovernanceAuthorityReader(
            session_factory=reader_sessions,
            dependency_port=ReadOnlyGovernanceDependencies(reader_sessions),
            scope_owner_resolvers={PUBLICATION_MODULE: InitialPublicationScopeResolver()},
        )
        publication_thin = ProductThinAuthorizationService(
            governance_basis_service=GovernanceAuthorizationBasisService(
                session_factory=reader_sessions, authority_reader=authority
            ),
            module_policies={PUBLICATION_MODULE: InitialPublicationPolicy(manifest)},
        )
        writer_settings = _role_settings(roles, school_id, anchor_id, writer=True)
        assert isinstance(writer_settings, ExplicitWriterSettings)
        writer_engine = build_school_fact_writer_engine(writer_settings)
        resources.callback(writer_engine.dispose)
        assert (
            verify_school_fact_writer_database(writer_engine, settings=writer_settings).alembic_head
            == "20260905_22"
        )

        def publication_service(selected_grant):
            selected_guard = InitialPublicationFinalGuard(
                manifest=manifest,
                scope_id=selected_grant.scope_id,
                grant_id=selected_grant.grant_id,
                thin_service=publication_thin,
            )
            selected_kernel = SqlAlchemySchoolFactWriterExecutorKernel(
                engine=writer_engine,
                settings=writer_settings,
                final_authorization_guard=selected_guard,
            )
            return InitialPublicationService(
                manifest=manifest,
                kernel=selected_kernel,
                grant_id=selected_grant.grant_id,
                require_current=selected_guard.require_current,
            )

        publication = publication_service(grant)
        current = identity.resolve_request(token)
        old_confirmation = None
        if recovery_mode == "expired":
            assert manifest.expires_at < datetime.now(UTC)
            with pytest.raises(SchoolFactChangeRejected):
                publication.prepare(current)
        elif recovery_mode == "concurrent":
            old_confirmation = publication.prepare(current)

        started = datetime.now(UTC)
        recovery = InitialPublicationRecoveryRequest(
            request_id=uuid4(),
            manifest_sha256=manifest.manifest_sha256,
            window_opened_at=started,
            window_expires_at=started + timedelta(minutes=5),
        )

        def recover():
            return recover_initial_publication(
                session_factory=migration_sessions, manifest=manifest, request=recovery
            )

        if recovery_mode != "direct":
            if recovery_mode == "concurrent":
                # Fault after the real old-Grant end must roll back that end and its epoch.
                def fail_create(*_args, **_kwargs):
                    raise RuntimeError("injected failure before replacement Grant")

                with monkeypatch.context() as patch:
                    patch.setattr(GovernanceWriter, "create_grant", fail_create)
                    with pytest.raises(RuntimeError, match="injected failure"):
                        recover()
                with migration_sessions() as session:
                    rolled_back = inspect_initial_publication(session, manifest=manifest)
                assert len(rolled_back.grants) == 1
                assert rolled_back.grants[0].grant_id == grant.grant_id
                assert rolled_back.grants[0].current_version == 1
                assert not rolled_back.grants[0].is_terminal
                gate = Barrier(2)

                def simultaneous_recover():
                    gate.wait(timeout=10)
                    return recover()

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(simultaneous_recover) for _ in range(2)]
                    renewed, replay = [future.result(timeout=30) for future in futures]
                assert renewed.grant == replay.grant
            else:
                renewed = recover()
                # Re-open the same operation as after a missing response; no second Grant.
                assert recover().grant == renewed.grant
            assert renewed.state == "window_recorded" and renewed.grant is not None
            assert renewed.grant.grant_id != grant.grant_id
            with migration_sessions() as session:
                state = inspect_initial_publication(session, manifest=manifest)
            assert len(state.grants) == 2
            assert sum(not item.is_terminal for item in state.grants) == 1
            with pytest.raises(SchoolFactChangeRejected):
                publication.prepare(identity.resolve_request(token))
            grant = renewed.grant
            publication = publication_service(grant)
            if old_confirmation is not None:
                with pytest.raises(SchoolFactChangeRejected):
                    publication.confirm(identity.resolve_request(token), old_confirmation)
        current = identity.resolve_request(token)
        confirmation = publication.prepare(current)
        published = publication.confirm(identity.resolve_request(token), confirmation)
        assert published.changed_count == 2
        lookup = publication.result(identity.resolve_request(token))
        assert lookup.found and lookup.result is not None
        assert lookup.result.batch_id == published.batch_id
        if recovery_mode == "direct":
            end_initial_publication_grant(
                session_factory=migration_sessions,
                manifest=manifest,
                grant=grant,
                dependency_port=dependencies,
            )
        else:
            # A new process sees the committed fact result and only closes authority.
            observed = recover()
            assert observed.state == "published"
            assert observed.applied_batch_id == published.batch_id and observed.grant is None
            assert recover() == observed
            with migration_sessions() as session:
                closed_publication = inspect_initial_publication(session, manifest=manifest)
            assert closed_publication.applied_batch_id == published.batch_id
            assert all(item.is_terminal for item in closed_publication.grants)

        current = identity.resolve_request(token)
        before = query.read_official_contacts(actor=current.actor, campus_name="合成主校区")
        assert before.status == "known" and len(before.contacts) == 1
        fact = before.contacts[0]
        assert fact.contact_value == "0571-00000000" and fact.source_kind == "manual"
        target = resolve_contact_target(query=query, actor=current.actor, fact_ref=fact.fact_ref)
        changed = CanonicalFactChangeSetV1(
            source_sha256=sha256(b"pytest:approved-contact-correction").hexdigest(),
            base_version=target.school_fact_version,
            changes=(
                CanonicalFactChangeV1(
                    operation="upsert",
                    fact_kind="official_contact",
                    source_key=target.source_key,
                    expected_version=target.version,
                    effective_at=datetime.now(UTC),
                    origin=FactOriginV1(sheet_code="official_contact_maintenance", row_number=1),
                    content=target.content.model_copy(update={"contact_value": "0571-00000001"}),
                ),
            ),
        )
        context = compose_command_context(current.actor, uuid4(), "pytest:f1:maintain")
        kernel = SqlAlchemySchoolFactWriterExecutorKernel(
            engine=writer_engine,
            settings=writer_settings,
            final_authorization_guard=guard,
        )
        preflight = kernel.preflight(
            school_id=current.actor.school_id,
            change_set=changed,
            context=context,
            session_binding=current.session_binding,
        )
        assert preflight.is_clean
        confirmed = kernel.prepare(
            change_set=changed,
            context=context,
            session_binding=current.session_binding,
            provenance=SchoolFactChangeProvenance(
                channel="admin",
                raw_source_sha256=changed.source_sha256,
                template_version="pytest_v1",
                mapping_version="pytest_v1",
                mapping_config_sha256="c" * 64,
                source_size_bytes=None,
                worksheet_names=(),
            ),
        )
        result = kernel.confirm(
            credential=confirmed.credential,
            change_set=changed,
            context=context,
            session_binding=current.session_binding,
        )
        assert result.changed_count == 1
        replayed = kernel.confirm(
            credential=confirmed.credential,
            change_set=changed,
            context=context,
            session_binding=current.session_binding,
        )
        assert replayed.batch_id == result.batch_id and replayed.replayed
        after = query.read_official_contacts(
            actor=identity.resolve_request(token).actor, campus_name="合成主校区"
        )
        assert after.status == "known"
        assert after.contacts[0].contact_value == "0571-00000001"
        assert after.contacts[0].version == fact.version + 1
        for parameters in (
            roles.runtime_connection_parameters,
            roles.reader_connection_parameters,
            roles.writer_connection_parameters,
        ):
            with connect(**parameters, autocommit=True) as connection:
                with pytest.raises(InsufficientPrivilege):
                    connection.execute("SELECT manifest_payload FROM school_fact_initializations")


@pytest.mark.integration
def test_f1_runtime_namespaced_identity_serializes_without_legacy_claim(
    school_facts_f1_schema: PostgresRoles,
) -> None:
    roles = school_facts_f1_schema
    school_id = uuid4()
    anchor_id = _root(roles, school_id)
    settings = _runtime_settings(roles, school_id, anchor_id)
    with roles.admin_connection() as connection:
        legacy = connection.execute(
            "INSERT INTO public.users(provider_code,provider_tenant_key,external_subject,"
            "display_name,is_active,security_version) "
            "VALUES('legacy','legacy','f1-maintainer','旧身份原名',false,7) "
            "RETURNING id,provider_code,provider_tenant_key,external_subject,display_name,"
            "is_active,security_version,created_at,updated_at"
        ).fetchone()
        assert legacy is not None
        connection.execute(
            "REVOKE EXECUTE ON FUNCTION "
            "app.claim_legacy_identity_user_id(text,text,text,text) FROM campus_ai_runtime"
        )
    engine = build_engine(settings)
    try:
        sessions = build_session_factory(engine)
        gate = Barrier(2)

        def create():
            with sessions() as session:
                principal = session.execute(
                    text(
                        "SELECT session_user,current_user,has_function_privilege("
                        "current_user,'app.claim_legacy_identity_user_id(text,text,text,text)',"
                        "'EXECUTE')"
                    )
                ).one()
                assert principal == ("campus_ai_runtime", "campus_ai_runtime", False)
                gate.wait(timeout=10)
                user = find_or_create_namespaced_test_user(
                    session, "f1-maintainer", "合成工程维护员"
                )
                return user.id

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create) for _ in range(2)]
            ids = [future.result(timeout=20) for future in futures]
        assert ids[0] == ids[1] and ids[0] != legacy[0]
        with roles.admin_connection() as connection:
            assert (
                connection.execute(
                    "SELECT id,provider_code,provider_tenant_key,external_subject,display_name,"
                    "is_active,security_version,created_at,updated_at "
                    "FROM public.users WHERE id=%s",
                    (legacy[0],),
                ).fetchone()
                == legacy
            )
        with sessions() as session:
            rows = session.execute(
                text(
                    "SELECT id,provider_code,provider_tenant_key,external_subject "
                    "FROM public.users "
                    "WHERE provider_code='local_test' AND provider_tenant_key='development' "
                    "AND external_subject='f1-maintainer'"
                )
            ).all()
            assert rows == [(ids[0], "local_test", "development", "f1-maintainer")]
    finally:
        engine.dispose()


_READER_WRAPPERS = (
    "app.school_fact_reader_assignment_terminal(uuid,uuid,bigint)",
    "app.school_fact_reader_identity_root_is_effective_at(uuid,text,uuid,timestamptz)",
)
_BROAD_HELPERS = (
    "app.school_fact_content_version_terminal(uuid,text,uuid,bigint)",
    "app.school_fact_root_is_effective_at(uuid,text,uuid,timestamptz)",
)


@pytest.mark.integration
def test_f1_revision22_reader_wrappers_exact_acl_anchor_arguments_and_bootstrap(
    school_facts_f1_schema: PostgresRoles,
) -> None:
    roles = school_facts_f1_schema
    school_id, target_id = uuid4(), uuid4()
    terminal_sql = "SELECT * FROM app.school_fact_reader_assignment_terminal(%s,%s,%s)"
    root_sql = "SELECT app.school_fact_reader_identity_root_is_effective_at(%s,%s,%s,%s)"
    now = datetime.now(UTC)
    # An absent physical anchor remains an unavailable source, even for the correct Reader.
    with connect(**roles.reader_connection_parameters, autocommit=True) as connection:
        with pytest.raises(Error) as absent:
            connection.execute(terminal_sql, (school_id, target_id, 1))
        assert absent.value.sqlstate == "55000"
    _root(roles, school_id)

    def assert_exact_acl():
        with roles.admin_connection() as connection:
            for signature in _READER_WRAPPERS:
                properties = connection.execute(
                    "SELECT owner.rolname,p.prosecdef,p.proconfig "
                    "FROM pg_proc p JOIN pg_roles owner ON owner.oid=p.proowner "
                    "WHERE p.oid=to_regprocedure(%s)",
                    (signature,),
                ).fetchone()
                assert properties == ("campus_ai_context_definer", True, ["search_path=pg_catalog"])
                grantees = connection.execute(
                    "SELECT COALESCE(r.rolname,'PUBLIC'),a.is_grantable FROM pg_proc p "
                    "CROSS JOIN LATERAL aclexplode(p.proacl) a "
                    "LEFT JOIN pg_roles r ON r.oid=a.grantee "
                    "WHERE p.oid=to_regprocedure(%s) AND a.privilege_type='EXECUTE' "
                    "ORDER BY 1",
                    (signature,),
                ).fetchall()
                assert grantees == [
                    ("campus_ai_context_definer", False),
                    ("campus_ai_school_fact_reader", False),
                ]
            for signature in _BROAD_HELPERS:
                assert connection.execute(
                    "SELECT has_function_privilege('campus_ai_school_fact_reader',%s,'EXECUTE')",
                    (signature,),
                ).fetchone() == (False,)

    assert_exact_acl()
    with connect(**roles.reader_connection_parameters, autocommit=True) as connection:
        assert connection.execute(terminal_sql, (school_id, target_id, 1)).fetchall() == []
        assert connection.execute(root_sql, (school_id, "person", target_id, now)).fetchone() == (
            False,
        )
        cases = [
            (terminal_sql, (uuid4(), target_id, 1), "28000"),
            (terminal_sql, (school_id, target_id, 0), "22023"),
            (terminal_sql, (school_id, None, 1), "22023"),
            (terminal_sql, (None, target_id, 1), "22023"),
            (root_sql, (uuid4(), "person", target_id, now), "28000"),
            (root_sql, (school_id, "student", target_id, now), "22023"),
            (root_sql, (school_id, None, target_id, now), "22023"),
            (root_sql, (school_id, "organization", target_id, None), "22023"),
        ]
        for statement, parameters, sqlstate in cases:
            with pytest.raises(Error) as rejected:
                connection.execute(cast(LiteralString, statement), parameters)
            assert rejected.value.sqlstate == sqlstate
        for statement in (
            "SELECT * FROM app.school_fact_content_version_terminal(%s,'assignment',%s,1)",
            "SELECT app.school_fact_root_is_effective_at(%s,'person',%s,clock_timestamp())",
        ):
            with pytest.raises(InsufficientPrivilege):
                connection.execute(statement, (school_id, target_id))
    for parameters in (
        roles.runtime_connection_parameters,
        roles.writer_connection_parameters,
        roles.migration_connection_parameters,
    ):
        with connect(**parameters, autocommit=True) as connection:
            for statement, arguments in (
                (terminal_sql, (school_id, target_id, 1)),
                (root_sql, (school_id, "person", target_id, now)),
            ):
                with pytest.raises(InsufficientPrivilege):
                    connection.execute(cast(LiteralString, statement), arguments)
    repeated = roles.rerun_bootstrap()
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert_exact_acl()


@pytest.mark.integration
def test_revision20_has_no_f1_reader_wrapper_capability(
    current_master_data_schema: PostgresRoles,
) -> None:
    with current_master_data_schema.admin_connection() as connection:
        for signature in _READER_WRAPPERS:
            assert connection.execute("SELECT to_regprocedure(%s)", (signature,)).fetchone() == (
                None,
            )


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["end", "revoke"])
def test_f1_reader_assignment_terminal_preserves_formal_historical_semantics(
    school_facts_f1_schema: PostgresRoles,
    operation: str,
) -> None:
    from app.platform.school_facts.application import SchoolFactChangeService
    from tests.school_facts.test_checkpoint2_lifecycle_red import (
        _confirm,
        _plan,
        _seed_all_facts,
        _terminal_change,
    )
    from tests.school_facts.test_sf2_transaction import NOW, sf2_environment

    roles = school_facts_f1_schema
    # Reuse the existing real canonical Writer test path; do not fabricate terminal ledger rows.
    with sf2_environment(roles) as (factory, persistence, facts):
        school_id, user_id, person_id, actor_assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        created = _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
        )
        with factory() as session:
            row = session.execute(
                text(
                    "SELECT id,person_id,organization_unit_id FROM staff_assignments "
                    "WHERE school_id=:school_id AND source_key='assignment:parent'"
                ),
                {"school_id": school_id},
            ).one()
        target_id, related_person_id, organization_id = row
        terminal_at = NOW + timedelta(days=1)
        payload = _terminal_change("assignment", "assignment:parent", 1, terminal_at, 2)
        payload["operation"] = operation
        _confirm(
            service=service,
            change_set=_plan(
                label=f"f1-wrapper-{operation}",
                base_version=created.target_version,
                changes=(payload,),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label=f"f1-wrapper-{operation}",
        )
        with connect(**roles.reader_connection_parameters, autocommit=True) as connection:
            assert connection.execute(
                "SELECT * FROM app.school_fact_reader_assignment_terminal(%s,%s,1)",
                (school_id, target_id),
            ).fetchall() == [(operation, terminal_at)]
            for kind, root_id in (("person", related_person_id), ("organization", organization_id)):
                assert connection.execute(
                    "SELECT app.school_fact_reader_identity_root_is_effective_at(%s,%s,%s,%s)",
                    (school_id, kind, root_id, NOW + timedelta(hours=1)),
                ).fetchone() == (True,)
        reader_settings = _role_settings(
            roles,
            school_id,
            _anchor_id(roles),
        )
        assert isinstance(reader_settings, ExplicitReaderSettings)
        engine = build_school_fact_reader_engine(reader_settings)
        try:
            dependency = ReadOnlyGovernanceDependencies(
                sessionmaker(engine, expire_on_commit=False)
            )
            before = dependency.read_assignment(
                school_id=school_id, assignment_id=target_id, as_of=NOW + timedelta(hours=1)
            )
            assert (before is None) == (operation == "revoke")
            assert (
                dependency.read_assignment(
                    school_id=school_id, assignment_id=target_id, as_of=terminal_at
                )
                is None
            )
        finally:
            engine.dispose()


def _anchor_id(roles: PostgresRoles) -> UUID:
    with roles.admin_connection() as connection:
        row = connection.execute("SELECT anchor_id FROM public.school_database_anchor").fetchone()
        assert row is not None
        return row[0]


@pytest.mark.integration
def test_f1_reader_contact_sources_do_not_inherit_runtime_context_policy(
    school_facts_f1_schema: PostgresRoles,
) -> None:
    with school_facts_f1_schema.admin_connection() as connection:
        policies = connection.execute(
            "SELECT tablename,policyname,roles,qual FROM pg_policies "
            "WHERE schemaname='public' AND tablename IN ("
            "'official_contact_points','campus_locations','campus_location_versions',"
            "'school_fact_sources','school_fact_change_lineage') "
            "AND cmd IN ('ALL','SELECT') ORDER BY tablename,policyname"
        ).fetchall()
    assert policies
    unsafe = [
        row
        for row in policies
        if ("public" in row[2] or "campus_ai_school_fact_reader" in row[2])
        and "require_school_context" in (row[3] or "")
    ]
    assert not unsafe, repr(unsafe)
