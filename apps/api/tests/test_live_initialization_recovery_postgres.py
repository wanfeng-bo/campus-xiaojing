"""Disposable PostgreSQL proof for the finite missing-school-version recovery.

Synthetic identity and school data only. No external login or deployed database is used.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from psycopg import sql
from sqlalchemy import create_engine, text

from app.composition.school_fact_deployment_root import controlled_school_root
from app.composition.school_fact_dingtalk_identity import (
    build_school_fact_dingtalk_identity_service,
)
from app.composition.school_fact_initialization import SchoolFactInitializer
from app.composition.school_fact_live_initialization import LiveFirstMaintainer
from app.composition.school_fact_live_initialization_contracts import (
    LiveInitializationConfiguration,
    LiveInitializationManifest,
    LiveInitializationMigration,
    LiveInitializationSession,
    LiveRootManifest,
)
from app.db.connection import build_engine, build_session_factory
from app.integrations.dingtalk.client import DingTalkIdentity
from app.platform.identity.context import CompleteIdentitySessionError, CurrentUser
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import ActorContextResolver
from app.platform.identity.service import find_or_create_dingtalk_user
from app.platform.identity.session import IdentitySessionStore
from app.platform.school_facts.initialization_contracts import SchoolFactInitializationRejected
from tests.master_data import conftest as shared
from tests.test_dingtalk_product_identity_postgres import (  # noqa: F401
    CONTEXT_KEY,
    identity_template,  # noqa: F811
)
from tests.test_f1_product_identity import MemoryRedis
from tests.test_live_initialization import configuration_payload, manifest_payload

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DINGTALK_POSTGRES_INTEGRATION") != "1",
    reason="explicit owned disposable PostgreSQL cluster required",
)


class _MemorySessionRedis(MemoryRedis):
    def close(self):
        pass


@contextmanager
def _four_fact_environment(template_fixture, monkeypatch):
    cluster, template = template_fixture
    cluster.verify_disposable_cluster()
    database = "campus_ai_school_facts_live_recovery_" + uuid4().hex[:16] + "_test"
    with cluster.admin_connection() as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER campus_ai_db_owner TEMPLATE {}").format(
                sql.Identifier(database),
                sql.Identifier(template.admin_connection_parameters["dbname"]),
            )
        )
    try:
        roles = shared._roles_for_database(template, database)
        now = datetime.now(UTC) - timedelta(seconds=1)
        school = uuid4()
        raw = manifest_payload()
        raw.update(
            publication_policy="deferred",
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
        assert len(raw["first_facts"]["changes"]) == 4
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
                key: raw[key]
                for key in (
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
        settings = LiveInitializationMigration.model_validate(configuration["migration"])
        engine = create_engine(settings.database_url, hide_parameters=True)
        try:
            anchor = controlled_school_root(engine, root, expected_database=database)
        finally:
            engine.dispose()
        raw["anchor_id"] = anchor["anchor_id"]
        configuration["runtime"].update(
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
        redis = _MemorySessionRedis()
        from app.composition import school_fact_initialization_readiness as readiness

        monkeypatch.setattr(readiness.Redis, "from_url", lambda *args, **kwargs: redis)
        runtime_engine = build_engine(config.runtime)
        try:
            sessions = build_session_factory(runtime_engine)
            with sessions.begin() as session:
                user = find_or_create_dingtalk_user(
                    session,
                    identity=DingTalkIdentity(
                        "test-corp", "test-first-operator", "合成身份提供者响应"
                    ),
                    request_id="synthetic-recovery-auth-fixture",
                )
                current = CurrentUser(
                    user.id, user.external_subject, user.display_name, user.security_version
                )
            token = IdentitySessionStore(redis=redis, ttl_seconds=28800).create(current)
        finally:
            runtime_engine.dispose()
        identity = LiveInitializationSession(
            token=token, corp_id="test-corp", external_subject="test-first-operator"
        )
        yield roles, root, config, manifest, identity, redis
    finally:
        cluster.verify_disposable_cluster()
        with cluster.admin_connection() as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )


def _original_snapshot(roles):
    with roles.admin_connection() as connection:
        return {
            "initialization": connection.execute(
                "SELECT id, state, manifest_sha256, result_payload "
                "FROM public.school_fact_initializations"
            ).fetchone(),
            "lineage": connection.execute(
                "SELECT fact_kind, fact_source_key, batch_id, unit_id, target_id, "
                "target_version FROM public.school_fact_change_lineage "
                "WHERE fact_kind <> 'school' ORDER BY fact_kind, fact_source_key"
            ).fetchall(),
            "grant_versions": connection.execute(
                "SELECT grant_id, version, valid_from, planned_valid_to, transition_kind "
                "FROM public.governance_grant_versions ORDER BY grant_id, version"
            ).fetchall(),
        }


def _staged_user_id(connection, batch_id):
    return connection.execute(
        "SELECT staged_by_user_id FROM public.master_data_import_batches WHERE id=%s",
        (batch_id,),
    ).fetchone()[0]


def test_four_fact_initialization_requires_exactly_one_reviewed_school_recovery(
    identity_template,  # noqa: F811
    monkeypatch,
):
    with _four_fact_environment(identity_template, monkeypatch) as (
        roles,
        root,
        config,
        manifest,
        identity,
        redis,
    ):
        with LiveFirstMaintainer(config, manifest, identity, redis=redis) as original:
            with pytest.raises(SchoolFactInitializationRejected):
                original.first_facts("prepare")
            with roles.admin_connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM public.school_fact_initializations"
                ).fetchone() == (0,)
                assert connection.execute(
                    "SELECT count(*) FROM public.master_data_import_batches"
                ).fetchone() == (0,)
            # Recreate the already-deployed historical state. The newly added
            # preflight gate would reject these four facts in a new deployment.
            with monkeypatch.context() as historic:
                historic.setattr(
                    SchoolFactInitializer,
                    "_require_school_version",
                    lambda *_args, **_kwargs: None,
                )
                prepared = original.first_facts("prepare")
                applied = original.first_facts("confirm", prepared["credential"])
            assert original.first_facts("confirm", prepared["credential"]) == applied
            grants = original.configure_contacts()
            before = _original_snapshot(roles)
            assert before["initialization"][1] == "applied"
            assert len(before["lineage"]) == 4
            assert len(before["grant_versions"]) == 2
            assert all(
                end - start == timedelta(days=90)
                for _grant, _version, start, end, _transition in before["grant_versions"]
            )
            with roles.admin_connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM public.school_versions"
                ).fetchone() == (0,)
                assert connection.execute(
                    "SELECT state FROM public.governance_bootstrap_states"
                ).fetchone() == ("open",)
            with pytest.raises(SchoolFactInitializationRejected):
                original.close(grants["scope_id"])
            identity_service = build_school_fact_dingtalk_identity_service(
                settings=config.runtime,
                session_factory=original.runtime_sessions,
                redis=redis,
                proof_signer=ContextProofSigner(config.runtime.rls_context_signing_key),
                can_maintain=lambda _actor: True,
            )
            assert (
                len(identity_service.session_view(identity.token.get_secret_value()).contexts) == 0
            )
            with roles.admin_connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM public.school_versions"
                ).fetchone() == (0,)
                assert connection.execute(
                    "SELECT state FROM public.governance_bootstrap_states"
                ).fetchone() == ("open",)

            from app.composition.school_fact_initialization_recovery import (
                LiveSchoolVersionRecovery,
            )
            from app.composition.school_fact_initialization_recovery_contracts import (
                SchoolVersionRecoveryManifest,
            )

            recovery_approved = datetime.now(UTC) - timedelta(seconds=1)
            recovery_raw = dict(
                purpose="school-facts-missing-school-version-recovery",
                school_id=manifest.school_id,
                anchor_id=manifest.anchor_id,
                expected_system_identifier=manifest.expected_system_identifier,
                database_name=manifest.database_name,
                initialization_id=UUID(str(before["initialization"][0])),
                recovery_request_id=uuid4(),
                original_manifest_sha256=manifest.manifest_sha256,
                corp_id=manifest.corp_id,
                external_subject=manifest.external_subject,
                school_source_key=root.source_key,
                school_code=root.school_code,
                school_display_name=root.display_name,
                timezone_name=root.timezone_name,
                approval_reference="school-reviewed:synthetic-missing-school-compensation",
                issuer_reference=manifest.issuer_reference,
                issuer_qualification_reference=manifest.issuer_qualification_reference,
                operator_reference=manifest.operator_reference,
                source_authority_reference=manifest.source_authority_reference,
                approved_at=recovery_approved,
                expires_at=recovery_approved + timedelta(minutes=15),
            )
            recovery = SchoolVersionRecoveryManifest.model_validate(recovery_raw)

            # A different school, reviewed digest, actor or root cannot borrow the
            # original four-fact receipt. Rejected preparations leave no new batch.
            for field, bad in (
                ("school_id", uuid4()),
                ("original_manifest_sha256", "0" * 64),
                ("external_subject", "another-operator"),
                ("school_source_key", "school:another"),
            ):
                forged = SchoolVersionRecoveryManifest.model_validate({**recovery_raw, field: bad})
                with pytest.raises(ValueError):
                    with LiveSchoolVersionRecovery(
                        config, manifest, forged, identity, redis=redis
                    ) as rejected:
                        rejected.prepare()
                with roles.admin_connection() as connection:
                    assert connection.execute(
                        "SELECT count(*) FROM public.master_data_import_batches"
                    ).fetchone() == (1,)
                    assert connection.execute(
                        "SELECT count(*) FROM public.school_versions"
                    ).fetchone() == (0,)

            clock_now = [datetime.now(UTC)]
            with LiveSchoolVersionRecovery(
                config,
                manifest,
                recovery,
                identity,
                redis=redis,
                clock=lambda: clock_now[0],
            ) as restored:
                with restored._transaction() as session:
                    record = restored._original_record(session)
                    account, _person, assignment, _version = restored._principal(session, record)
                    with monkeypatch.context() as role_failure:

                        def rejected_options(*_args, **_kwargs):
                            raise RuntimeError("synthetic resolver failure")

                        role_failure.setattr(ActorContextResolver, "list_options", rejected_options)
                        with pytest.raises(RuntimeError, match="synthetic resolver failure"):
                            restored._resolve_restored_actor(session, account, assignment)
                    assert session.scalar(text("SELECT current_user")) == "campus_ai_db_owner"

                recovery_prepared = restored.prepare()
                assert recovery_prepared["state"] == "prepared"
                assert restored.result()["state"] == "prepared"
                competing = SchoolVersionRecoveryManifest.model_validate(
                    {
                        **recovery_raw,
                        "recovery_request_id": uuid4(),
                        "approval_reference": "school-reviewed:synthetic-competing-recovery",
                    }
                )
                with LiveSchoolVersionRecovery(
                    config, manifest, competing, identity, redis=redis
                ) as competitor:
                    competing_prepared = competitor.prepare()
                    assert competing_prepared["batch_id"] != recovery_prepared["batch_id"]
                    assert competitor.result()["state"] == "prepared"
                with pytest.raises(ValueError):
                    restored.confirm(credential="incorrect-credential")

                clock_now[0] = recovery.expires_at
                with pytest.raises(ValueError):
                    restored.confirm(credential=recovery_prepared["credential"])
                assert restored.result()["state"] == "prepared"
                clock_now[0] = datetime.now(UTC)

                with roles.admin_connection() as connection:
                    staged_user_id = _staged_user_id(connection, recovery_prepared["batch_id"])
                    connection.execute(
                        "UPDATE public.users SET is_active=false WHERE id=%s",
                        (staged_user_id,),
                    )
                try:
                    with pytest.raises(CompleteIdentitySessionError):
                        restored.confirm(credential=recovery_prepared["credential"])
                    with roles.admin_connection() as connection:
                        assert connection.execute(
                            "SELECT count(*) FROM public.school_versions"
                        ).fetchone() == (0,)
                finally:
                    with roles.admin_connection() as connection:
                        connection.execute(
                            "UPDATE public.users SET is_active=true WHERE id=%s",
                            (staged_user_id,),
                        )

                # Failure after the canonical school insert must roll back the
                # version, lineage and terminal receipt together; retry is safe.
                with monkeypatch.context() as patch:

                    def interrupted_identity(*_args, **_kwargs):
                        raise RuntimeError("synthetic recovery interruption")

                    patch.setattr(restored, "_resolve_restored_actor", interrupted_identity)
                    with pytest.raises(RuntimeError, match="synthetic recovery interruption"):
                        restored.confirm(credential=recovery_prepared["credential"])
                with roles.admin_connection() as connection:
                    assert connection.execute(
                        "SELECT count(*) FROM public.school_versions"
                    ).fetchone() == (0,)
                    assert connection.execute(
                        "SELECT count(*) FROM public.school_fact_change_lineage"
                    ).fetchone() == (4,)
                    assert connection.execute(
                        "SELECT status FROM public.master_data_import_batches WHERE id=%s",
                        (recovery_prepared["batch_id"],),
                    ).fetchone() == ("ready_for_confirmation",)

                confirmed = restored.confirm(credential=recovery_prepared["credential"])
                assert confirmed["batch_id"] == recovery_prepared["batch_id"]
                assert confirmed["target_version"] == 3
                assert confirmed["changed_count"] == 1
                assert confirmed["replayed"] is False
                replayed = restored.confirm(credential=recovery_prepared["credential"])
                assert replayed["batch_id"] == confirmed["batch_id"]
                assert replayed["changed_count"] == 1
                assert replayed["replayed"] is True
                assert restored.result()["state"] == "applied"

            with LiveSchoolVersionRecovery(
                config, manifest, competing, identity, redis=redis
            ) as competitor:
                with pytest.raises(ValueError):
                    competitor.confirm(credential=competing_prepared["credential"])
                assert competitor.result()["state"] == "prepared"
            with LiveSchoolVersionRecovery(
                config, manifest, recovery, identity, redis=redis
            ) as restored:
                again = restored.prepare()
                assert again["batch_id"] == confirmed["batch_id"]
                assert again["state"] == "applied"
                assert again["credential"] is None

            after = _original_snapshot(roles)
            assert after == before
            with roles.admin_connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM public.school_versions"
                ).fetchone() == (1,)
                assert connection.execute(
                    "SELECT count(*) FROM public.school_fact_change_lineage"
                ).fetchone() == (5,)
                assert connection.execute(
                    "SELECT source_kind, source_ref, mapping_payload->>'channel' "
                    "FROM public.master_data_import_batches WHERE id=%s",
                    (confirmed["batch_id"],),
                ).fetchone() == (
                    "manual",
                    f"initialization-school-recovery:{recovery.recovery_request_id}",
                    "deployment_missing_school_version_recovery",
                )
                assert connection.execute(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE event_type='school_facts.deployment_recovery.applied' "
                    "AND target_id=%s",
                    (str(confirmed["batch_id"]),),
                ).fetchone() == (1,)

            # An earlier pre-recovery UI visit cached zero options for this
            # login nonce. Raw runtime enumeration now sees exactly one; formal
            # closure must use fresh DB identity without mutating UI state or
            # requiring a replacement login token.
            account = original._account()
            raw_options = ActorContextResolver(
                session_factory=original.runtime_sessions,
                redis=redis,
                proof_signer=ContextProofSigner(config.runtime.rls_context_signing_key),
                current_user=account.current_user,
                identity_session=account.identity_session,
            ).list_options()
            assert len(raw_options) == 1
            assert raw_options[0].kind == "staff_assignment"
            cached_view = identity_service.session_view(identity.token.get_secret_value())
            assert len(cached_view.contexts) == 0
            with roles.admin_connection() as connection:
                connection.execute(
                    "UPDATE public.users SET is_active=false WHERE id=%s",
                    (account.current_user.id,),
                )
            try:
                with pytest.raises(CompleteIdentitySessionError):
                    original.close(grants["scope_id"])
                with roles.admin_connection() as connection:
                    assert connection.execute(
                        "SELECT state FROM public.governance_bootstrap_states"
                    ).fetchone() == ("open",)
            finally:
                with roles.admin_connection() as connection:
                    connection.execute(
                        "UPDATE public.users SET is_active=true WHERE id=%s",
                        (account.current_user.id,),
                    )
            assert original.close(grants["scope_id"])["state"] == "closed"
            assert (
                len(identity_service.session_view(identity.token.get_secret_value()).contexts) == 0
            )
            assert original.close(grants["scope_id"])["state"] == "closed"
            with roles.admin_connection() as connection:
                assert connection.execute(
                    "SELECT state FROM public.school_fact_initializations"
                ).fetchone() == ("closed",)
                assert connection.execute(
                    "SELECT state FROM public.governance_bootstrap_states"
                ).fetchone() == ("closed",)
                assert connection.execute(
                    "SELECT count(*) FROM public.school_versions"
                ).fetchone() == (1,)
