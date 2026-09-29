"""Owned PG16 first-maintainer chain; synthetic DingTalk response and MemoryRedis only.

No external authentication, real school data, live Redis, or production deployment
is represented by this test. Domain writes run only in a new disposable database.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from psycopg import sql
from sqlalchemy import create_engine

from app.composition.school_fact_deployment_root import controlled_school_root
from app.composition.school_fact_initial_publication import (
    InitialPublicationManifest,
    PublicationGrantReceipt,
)
from app.composition.school_fact_live_initialization import (
    LiveFirstMaintainer,
    load_reviewed_contract,
)
from app.composition.school_fact_live_initialization_contracts import (
    LiveInitializationConfiguration,
    LiveInitializationManifest,
    LiveInitializationSession,
    LiveRootManifest,
)
from app.db.connection import build_engine, build_session_factory
from app.integrations.dingtalk.client import DingTalkIdentity
from app.platform.governance.application import GovernanceWriter
from app.platform.identity.context import CompleteIdentitySessionError, CurrentUser
from app.platform.identity.service import find_or_create_dingtalk_user
from app.platform.identity.session import IdentitySessionStore
from app.platform.school_facts.contracts import ConfirmationReceiptV1
from app.platform.school_facts.initialization_contracts import SchoolFactInitializationRejected
from tests.master_data import conftest as shared
from tests.school_facts.test_sf2_migration import _run_alembic
from tests.test_dingtalk_product_identity_postgres import (  # noqa: F401
    CONTEXT_KEY,
    identity_template,  # noqa: F811
)
from tests.test_f1_initial_publication import change_set as publication_facts
from tests.test_f1_product_identity import MemoryRedis
from tests.test_live_initialization import configuration_payload, manifest_payload

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DINGTALK_POSTGRES_INTEGRATION") != "1",
    reason="explicit owned disposable PostgreSQL cluster required",
)


def test_formal_initialization_closes_and_preserves_upgrade(
    identity_template,  # noqa: F811 -- shared owned-cluster fixture
    monkeypatch,
    tmp_path,
    publication_policy="required",
    closure_control=None,
    on_upgraded=None,
):
    __tracebackhide__ = True
    cluster, template = identity_template
    cluster.verify_disposable_cluster()
    database = "campus_ai_school_facts_live_" + uuid4().hex[:20] + "_test"
    with cluster.admin_connection() as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER campus_ai_db_owner TEMPLATE {}").format(
                sql.Identifier(database),
                sql.Identifier(template.admin_connection_parameters["dbname"]),
            )
        )
    roles = shared._roles_for_database(template, database)
    now = datetime.now(UTC) - timedelta(seconds=1)
    school = uuid4()
    raw = manifest_payload()
    raw.update(
        publication_policy=publication_policy,
        school_id=school,
        scope_school_id=school,
        database_name=database,
        request_id=uuid4(),
        expected_system_identifier=roles.disposable_cluster_system_identifier,
        approved_at=now,
        expires_at=now + timedelta(minutes=15),
        grant_valid_from=now,
        grant_valid_to=now + timedelta(days=90),
    )
    for change in raw["first_facts"]["changes"]:
        change["effective_at"] = now
    raw["first_facts"]["changes"] = (
        dict(
            operation="upsert",
            fact_kind="school",
            source_key="school:formal-test",
            expected_version=1,
            effective_at=now,
            origin=dict(sheet_code="school", row_number=1),
            content=dict(
                business_code="FORMAL-TEST",
                display_name="合成正式接线测试学校",
                timezone_name="Asia/Shanghai",
                lifecycle_status="active",
            ),
        ),
        *raw["first_facts"]["changes"],
    )
    root = LiveRootManifest(
        purpose="school-facts-live-root",
        db=database,
        system_identifier=roles.disposable_cluster_system_identifier,
        school_id=school,
        source_key="school:formal-test",
        school_code="FORMAL-TEST",
        display_name="合成正式接线测试学校",
        timezone_name="Asia/Shanghai",
        **{
            k: raw[k]
            for k in (
                "approval_reference",
                "issuer_reference",
                "issuer_qualification_reference",
                "operator_reference",
                "source_authority_reference",
                "approved_at",
                "expires_at",
            )
        },
    )
    configuration = configuration_payload()
    migration = roles.migration_connection_parameters
    configuration["migration"].update(
        host=migration["host"],
        port=migration["port"],
        db=database,
        password=migration["password"],
        context_signing_key=CONTEXT_KEY.decode(),
    )
    from app.composition.school_fact_live_initialization_contracts import (
        LiveInitializationMigration,
    )

    migration_settings = LiveInitializationMigration.model_validate(configuration["migration"])
    engine = create_engine(migration_settings.database_url, hide_parameters=True)
    try:
        anchor = controlled_school_root(engine, root, expected_database=database)
        assert (
            controlled_school_root(engine, root, expected_database=database, inspect_only=True)
            == anchor
        )
    finally:
        engine.dispose()
    raw["anchor_id"] = anchor["anchor_id"]
    runtime = configuration["runtime"]
    runtime.update(
        postgres_host=migration["host"],
        postgres_port=migration["port"],
        postgres_db=database,
        postgres_password=roles.runtime_connection_parameters["password"],
        postgres_expected_school_id=school,
        postgres_expected_anchor_id=anchor["anchor_id"],
        postgres_expected_system_identifier=roles.disposable_cluster_system_identifier,
        rls_context_signing_key=CONTEXT_KEY.decode(),
    )
    for kind in ("reader", "writer"):
        configuration[kind].update(
            database_host=migration["host"],
            database_port=migration["port"],
            database_name=database,
            expected_school_id=school,
            expected_anchor_id=anchor["anchor_id"],
            expected_system_identifier=roles.disposable_cluster_system_identifier,
            database_password=getattr(roles, kind + "_connection_parameters")["password"],
        )
    configuration["writer"]["rls_context_signing_key"] = CONTEXT_KEY.decode()
    config = LiveInitializationConfiguration.model_validate(configuration)
    manifest = LiveInitializationManifest.model_validate(raw)

    class RedisFixture(MemoryRedis):
        def close(self):
            pass

    redis = RedisFixture()
    from app.composition import school_fact_initialization_readiness as readiness

    monkeypatch.setattr(readiness.Redis, "from_url", lambda *args, **kwargs: redis)
    runtime_engine = build_engine(config.runtime)
    try:
        sessions = build_session_factory(runtime_engine)
        with sessions.begin() as session:
            user = find_or_create_dingtalk_user(
                session,
                identity=DingTalkIdentity("test-corp", "test-first-operator", "合成身份提供者响应"),
                request_id="synthetic-auth-fixture",
            )
            current = CurrentUser(
                user.id, user.external_subject, user.display_name, user.security_version
            )
        token = IdentitySessionStore(redis=redis, ttl_seconds=28800).create(current)
    finally:
        runtime_engine.dispose()
    supplied = LiveInitializationSession(
        token=token, corp_id="test-corp", external_subject="test-first-operator"
    )
    try:
        with LiveFirstMaintainer(config, manifest, supplied, redis=redis) as adapter:
            prepared = adapter.first_facts("prepare")
            for column, denied, restored in (
                ("is_active", False, True),
                ("security_version", 2, 1),
            ):
                original_check = adapter._check

                def invalidate_after_check(
                    action, original_check=original_check, column=column, denied=denied
                ):
                    original_check(action)
                    with roles.admin_connection() as connection:
                        connection.execute(
                            sql.SQL("UPDATE public.users SET {}=%s WHERE id=%s").format(
                                sql.Identifier(column)
                            ),
                            (denied, current.id),
                        )

                try:
                    with monkeypatch.context() as patch:
                        patch.setattr(adapter, "_check", invalidate_after_check)
                        with pytest.raises(CompleteIdentitySessionError):
                            adapter.first_facts("confirm", prepared["credential"])
                    with roles.admin_connection() as connection:
                        assert connection.execute(
                            "SELECT count(*) FROM public.staff_assignments"
                        ).fetchone() == (0,)
                finally:
                    with roles.admin_connection() as connection:
                        connection.execute(
                            sql.SQL("UPDATE public.users SET {}=%s WHERE id=%s").format(
                                sql.Identifier(column)
                            ),
                            (restored, current.id),
                        )
            applied = adapter.first_facts("confirm", prepared["credential"])
            assert adapter.first_facts("confirm", prepared["credential"]) == applied
            original_scope = GovernanceWriter.create_scope

            def logout_after_scope(writer, command):
                scope = original_scope(writer, command)
                IdentitySessionStore(redis=redis, ttl_seconds=28800).delete(token)
                return scope

            with monkeypatch.context() as patch:
                patch.setattr(GovernanceWriter, "create_scope", logout_after_scope)
                with pytest.raises(CompleteIdentitySessionError):
                    adapter.configure_contacts()
            with roles.admin_connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM public.governance_grants"
                ).fetchone() == (0,)
            # Renew only the synthetic login fixture after explicit logout. This is
            # not a deployment grant or a sliding manifest/confirmation deadline.
            token = IdentitySessionStore(redis=redis, ttl_seconds=28800).create(current)
            adapter.identity = LiveInitializationSession(
                token=token, corp_id="test-corp", external_subject="test-first-operator"
            )
            contacts = adapter.configure_contacts()
            original = adapter.first_facts("result")
            changes = publication_facts().model_dump(mode="python", exclude_none=True)
            for change in changes["changes"]:
                change["effective_at"] = now
            publication = InitialPublicationManifest(
                school_id=school,
                initialization_id=UUID(original["initialization_id"]),
                assignment_id=contacts["assignment_id"],
                request_id=uuid4(),
                approved_at=now,
                expires_at=manifest.expires_at,
                approval_reference="synthetic-reviewed-publication",
                change_set=changes,
            )
            if publication_policy == "required":
                grant = PublicationGrantReceipt(
                    **adapter.publication("publication-grant", publication)
                )
                confirmation_path = tmp_path / "publication-confirmation.json"
                confirmation_path.write_text(
                    json.dumps(adapter.publication("publication-prepare", publication, grant)),
                    encoding="utf-8",
                )
                confirmation = load_reviewed_contract(ConfirmationReceiptV1, confirmation_path)
                published = adapter.publication(
                    "publication-confirm", publication, grant, confirmation
                )
                assert published["changed_count"] == 2
                # After the deadline, original closure/results remain available; no new grants.
                adapter.clock = lambda: manifest.expires_at
                with pytest.raises(ValueError):
                    adapter.configure_contacts()
                adapter.publication("publication-end", publication, grant)
                assert (
                    adapter.publication("publication-result", publication, grant)["found"] is True
                )
            else:
                from app.platform.school_facts.application import SchoolFactChangeRejected

                for action in ("publication-grant", "publication-prepare", "publication-confirm"):
                    with pytest.raises(ValueError, match="deferred"):
                        adapter.publication(action, publication)
                with roles.admin_connection() as connection:
                    assert connection.execute(
                        "SELECT count(*) FROM public.campus_locations"
                    ).fetchone() == (0,)
                    assert connection.execute(
                        "SELECT count(*) FROM public.official_contact_points"
                    ).fetchone() == (0,)
                original_precheck = (
                    readiness.FirstMaintainerReadiness.verify_before_governance_close
                )
                if closure_control in {"read-revoked", "maintain-revoked"}:
                    from app.composition.governance_dependencies import (
                        SqlAlchemyGovernanceDependencyAdapter,
                    )
                    from app.platform.governance.contracts import RevokeGrantCommand
                    from app.platform.school_facts.read_policy import (
                        SCHOOL_SCOPE_OWNER,
                        SingleSchoolScopeResolver,
                    )

                    target = contacts["grants"][0 if closure_control == "read-revoked" else 1]

                    class SyntheticRevokeAuthorizer:
                        def authorize(self, request):
                            return (
                                request.school_id == school
                                and request.command_name == "governance.grant.revoke"
                                and request.target_id == target["target_id"]
                            )

                    revoker = GovernanceWriter(
                        session_factory=adapter.dependency_sessions,
                        authorizer=SyntheticRevokeAuthorizer(),
                        scope_owner_resolvers={SCHOOL_SCOPE_OWNER: SingleSchoolScopeResolver()},
                        clock=lambda: datetime.now(UTC),
                        dependency_port=SqlAlchemyGovernanceDependencyAdapter(
                            adapter.dependency_sessions
                        ),
                    )

                    def revoke_after_precheck(verifier, request, result):
                        original_precheck(verifier, request, result)
                        revoker.revoke_grant(
                            RevokeGrantCommand(
                                school_id=school,
                                grant_id=target["target_id"],
                                expected_version=target["target_version"],
                                effective_at=datetime.now(UTC),
                                actor=adapter._actor("synthetic-revoke"),
                                idempotency_key="synthetic-revoke:" + closure_control,
                            )
                        )

                    with monkeypatch.context() as patch:
                        patch.setattr(
                            readiness.FirstMaintainerReadiness,
                            "verify_before_governance_close",
                            revoke_after_precheck,
                        )
                        with pytest.raises(
                            (SchoolFactInitializationRejected, SchoolFactChangeRejected)
                        ):
                            adapter.close(contacts["scope_id"])
                    with roles.admin_connection() as connection:
                        assert connection.execute(
                            "SELECT state FROM public.governance_bootstrap_states"
                        ).fetchone() == ("open",)
                        assert connection.execute(
                            "SELECT state FROM public.school_fact_initializations"
                        ).fetchone() == ("applied",)
                    return
                if closure_control == "residual":

                    def start_publication_after_precheck(verifier, request, result):
                        original_precheck(verifier, request, result)
                        from app.composition.school_fact_initial_publication import (
                            configure_initial_publication_grant,
                        )

                        configure_initial_publication_grant(adapter._writer(), manifest=publication)

                    with monkeypatch.context() as patch:
                        patch.setattr(
                            readiness.FirstMaintainerReadiness,
                            "verify_before_governance_close",
                            start_publication_after_precheck,
                        )
                        with pytest.raises(SchoolFactChangeRejected):
                            adapter.close(contacts["scope_id"])
                    with roles.admin_connection() as connection:
                        assert connection.execute(
                            "SELECT state FROM public.governance_bootstrap_states"
                        ).fetchone() == ("open",)
                        assert connection.execute(
                            "SELECT state FROM public.school_fact_initializations"
                        ).fetchone() == ("applied",)
                    return
                if closure_control == "concurrent":
                    from concurrent.futures import ThreadPoolExecutor
                    from threading import Event, current_thread

                    from app.composition.governance_dependencies import (
                        SqlAlchemyGovernanceDependencyAdapter,
                    )
                    from app.composition.school_fact_initial_publication import (
                        PUBLICATION_MODULE,
                        InitialPublicationScopeResolver,
                        configure_initial_publication_grant,
                    )
                    from app.composition.school_fact_live_initialization import _FiniteBootstrap
                    from app.platform.governance.bootstrap import build_migration_bootstrap_writer
                    from app.platform.governance.contracts import GovernanceBootstrapClosed
                    from app.platform.governance.repository import GovernanceRepository

                    independent_writer = build_migration_bootstrap_writer(
                        session_factory=adapter.dependency_sessions,
                        clock=lambda: datetime.now(UTC),
                        dependency_port=SqlAlchemyGovernanceDependencyAdapter(
                            adapter.dependency_sessions
                        ),
                        scope_owner_resolvers={
                            PUBLICATION_MODULE: InitialPublicationScopeResolver()
                        },
                    )
                    attempted = Event()
                    original_lock = GovernanceRepository.lock_bootstrap_state
                    original_verify = _FiniteBootstrap._verify_closure
                    futures = []

                    def observed_lock(repository, school_id):
                        if current_thread().name.startswith("publication-race"):
                            attempted.set()
                        return original_lock(repository, school_id)

                    with ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="publication-race"
                    ) as pool:

                        def race_after_lock(authorizer, repository, request):
                            if request.command_name == "governance.bootstrap.close" and not futures:
                                futures.append(
                                    pool.submit(
                                        configure_initial_publication_grant,
                                        independent_writer,
                                        manifest=publication,
                                    )
                                )
                                assert attempted.wait(5), (
                                    "concurrent Writer did not attempt the state lock"
                                )
                                assert not futures[0].done()
                            original_verify(authorizer, repository, request)

                        with monkeypatch.context() as patch:
                            patch.setattr(
                                GovernanceRepository, "lock_bootstrap_state", observed_lock
                            )
                            patch.setattr(_FiniteBootstrap, "_verify_closure", race_after_lock)
                            assert adapter.close(contacts["scope_id"])["state"] == "closed"
                            with pytest.raises(GovernanceBootstrapClosed):
                                futures[0].result(timeout=10)
                    with roles.admin_connection() as connection:
                        assert connection.execute(
                            "SELECT count(*) FROM public.governance_duties "
                            "WHERE duty_code='initial_school_fact_publisher'"
                        ).fetchone() == (0,)
                    return

                # Holding the inverse-order epoch lock must reject immediately,
                # without waiting for a publication-end/bootstrap deadlock.
                from sqlalchemy.exc import DBAPIError

                with roles.admin_connection() as epoch_connection:
                    with epoch_connection.transaction():
                        epoch_connection.execute(
                            "SELECT epoch FROM public.governance_assignment_epochs "
                            "WHERE school_id=%s AND assignment_id=%s FOR UPDATE",
                            (school, contacts["assignment_id"]),
                        ).fetchone()
                        with pytest.raises(DBAPIError) as contention:
                            adapter.close(contacts["scope_id"])
                        assert contention.value.orig.sqlstate == "55P03"
                with roles.admin_connection() as connection:
                    assert connection.execute(
                        "SELECT state FROM public.governance_bootstrap_states"
                    ).fetchone() == ("open",)

                # A revoked real account between precheck and closure must not
                # close either authority. Restore only this synthetic fixture.
                def disable_after_precheck(verifier, request, result):
                    original_precheck(verifier, request, result)
                    with roles.admin_connection() as connection:
                        connection.execute(
                            "UPDATE public.users SET is_active=false WHERE id=%s", (current.id,)
                        )

                try:
                    with monkeypatch.context() as patch:
                        patch.setattr(
                            readiness.FirstMaintainerReadiness,
                            "verify_before_governance_close",
                            disable_after_precheck,
                        )
                        with pytest.raises(CompleteIdentitySessionError):
                            adapter.close(contacts["scope_id"])
                    with roles.admin_connection() as connection:
                        assert connection.execute(
                            "SELECT state FROM public.governance_bootstrap_states"
                        ).fetchone() == ("open",)
                finally:
                    with roles.admin_connection() as connection:
                        connection.execute(
                            "UPDATE public.users SET is_active=true WHERE id=%s", (current.id,)
                        )
                # Simulate a process failure between the two committed closes.
                original_close = adapter.initializer.close
                with monkeypatch.context() as patch:

                    def interrupted_close(*args, **kwargs):
                        raise RuntimeError("synthetic process interruption")

                    patch.setattr(adapter.initializer, "close", interrupted_close)
                    with pytest.raises(RuntimeError, match="synthetic process"):
                        adapter.close(contacts["scope_id"])
                with roles.admin_connection() as connection:
                    assert connection.execute(
                        "SELECT state FROM public.governance_bootstrap_states"
                    ).fetchone() == ("closed",)
                    assert connection.execute(
                        "SELECT state FROM public.school_fact_initializations"
                    ).fetchone() == ("applied",)
                assert adapter.initializer.close == original_close
            assert adapter.close(contacts["scope_id"])["state"] == "closed"
            assert adapter.close(contacts["scope_id"])["state"] == "closed"
            assert IdentitySessionStore(redis=redis, ttl_seconds=28800).read(token) is not None
            assert adapter.first_facts("result")["state"] == "closed"
            adapter.clock = lambda: datetime.now(UTC)
            with pytest.raises(SchoolFactInitializationRejected):
                adapter.first_facts("prepare")
            with pytest.raises(ValueError):
                adapter.configure_contacts()
        with roles.admin_connection() as connection:
            grants = connection.execute(
                "SELECT planned_valid_to-valid_from FROM public.governance_grant_versions "
                "WHERE transition_kind='create' ORDER BY planned_valid_to-valid_from"
            ).fetchall()
            expected = [timedelta(days=90), timedelta(days=90)]
            if publication_policy == "required":
                expected.insert(0, timedelta(minutes=15))
            assert [x[0] for x in grants] == expected
            before = connection.execute(
                "SELECT count(*) FROM public.school_fact_change_lineage"
            ).fetchone()
        upgraded = _run_alembic(roles, "upgrade", "20260924_56")
        if upgraded.returncode:
            pytest.fail(
                shared._redact(upgraded.stdout + upgraded.stderr, roles.protected_test_secrets)[
                    -1800:
                ],
                pytrace=False,
            )
        with roles.admin_connection() as connection:
            assert connection.execute(
                "SELECT state FROM public.school_fact_initializations"
            ).fetchone() == ("closed",)
            assert connection.execute(
                "SELECT state FROM public.governance_bootstrap_states"
            ).fetchone() == ("closed",)
            assert (
                connection.execute(
                    "SELECT count(*) FROM public.school_fact_change_lineage"
                ).fetchone()
                == before
            )
        if on_upgraded is not None:
            on_upgraded(roles)
    finally:
        cluster.verify_disposable_cluster()
        with cluster.admin_connection() as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )


def test_deferred_initialization_closes_without_publication_and_preserves_upgrade(
    identity_template,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    test_formal_initialization_closes_and_preserves_upgrade(
        identity_template, monkeypatch, tmp_path, publication_policy="deferred"
    )


def test_deferred_initialization_rejects_publication_started_after_precheck(
    identity_template,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    for control in ("residual", "read-revoked", "maintain-revoked"):
        test_formal_initialization_closes_and_preserves_upgrade(
            identity_template,
            monkeypatch,
            tmp_path,
            publication_policy="deferred",
            closure_control=control,
        )


def test_deferred_initialization_serializes_concurrent_publication(
    identity_template,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    test_formal_initialization_closes_and_preserves_upgrade(
        identity_template,  # noqa: F811
        monkeypatch,
        tmp_path,
        publication_policy="deferred",
        closure_control="concurrent",
    )
