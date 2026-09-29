"""Synthetic owned PG16 validation: closed schema22 initialization through schema56 live batch.

Reuses the existing production initialization exercise; no original database,
real qualification, DingTalk network exchange, model call or deployment is involved.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from psycopg import sql

from app.composition import school_fact_live_governance_cli as api
from tests import test_live_initialization_postgres as initialization
from tests.test_dingtalk_product_identity_postgres import identity_template  # noqa: F401
from tests.test_live_initialization import configuration_payload

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DINGTALK_POSTGRES_INTEGRATION") != "1",
    reason="explicit new owned disposable PostgreSQL cluster required",
)


def test_live_school_scope_writer_read_update_and_revoke(identity_template, monkeypatch, tmp_path):  # noqa: F811
    adapters = []
    real = initialization.LiveFirstMaintainer

    def capture(*args, **kwargs):
        adapter = real(*args, **kwargs)
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(initialization, "LiveFirstMaintainer", capture)

    def upgraded(roles):
        result = initialization._run_alembic(roles, "upgrade", "20260928_57")
        assert result.returncode == 0, "Isolated schema57 migration failed"
        _exercise_school_scope(roles, adapters[0], tmp_path)

    initialization.test_formal_initialization_closes_and_preserves_upgrade(
        identity_template,
        monkeypatch,
        tmp_path,
        publication_policy="deferred",
        on_upgraded=upgraded,
    )


def _exercise_school_scope(roles, adapter, tmp_path):
    """Use the closed first maintainer's current identity and exact post-close F2 grants."""
    __tracebackhide__ = True
    from sqlalchemy.orm import sessionmaker

    from app.composition.school_fact_dingtalk_identity import (
        build_school_fact_dingtalk_identity_service,
    )
    from app.composition.school_fact_directory_services import create_directory_read_components
    from app.composition.school_fact_directory_settings import (
        DirectoryConfiguration,
        DirectoryScopeBinding,
    )
    from app.composition.school_fact_family_authority import (
        F3OwnResultAuthorizer,
        create_family_authority_components,
    )
    from app.composition.school_fact_family_execution import (
        FamilyFinalAuthorizationGuard,
        FamilyWriterDispatch,
        PartitionedOwnResultAuthorizer,
    )
    from app.composition.school_fact_family_settings import FamilyConfiguration
    from app.composition.school_fact_package_authorization import (
        PackageExistingQualityKind,
        PackageFactPartition,
        PackageReferenceAuthorization,
        PackageWriterAuthorization,
    )
    from app.composition.school_fact_reader import (
        SchoolFactReaderSettings,
        build_school_fact_reader_engine,
    )
    from app.composition.school_fact_writer_executor import (
        SqlAlchemySchoolFactWriterExecutorKernel,
    )
    from app.composition.school_fact_writer_settings import (
        SchoolFactWriterSettings,
        build_school_fact_writer_engine,
    )
    from app.core.settings import Settings
    from app.db.connection import build_engine, build_session_factory
    from app.platform.identity.context_proof import ContextProofSigner
    from app.platform.identity.execution_context import compose_command_context
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
    from app.platform.school_facts.ports import SchoolFactChangeProvenance

    roles.verify_disposable_cluster()
    school = adapter.manifest.school_id
    with roles.admin_connection() as connection:
        assert connection.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            "20260928_57",
        )
        assert connection.execute(
            "SELECT state FROM public.school_fact_initializations"
        ).fetchone() == ("closed",)
        assert connection.execute(
            "SELECT state FROM public.governance_bootstrap_states"
        ).fetchone() == ("closed",)
        assert connection.execute("SELECT count(*) FROM public.campus_locations").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM public.official_contact_points"
        ).fetchone() == (0,)
        initial = connection.execute(
            "SELECT id,request_id,state,result_payload,closed_at "
            "FROM public.school_fact_initializations"
        ).fetchone()
        close = connection.execute(
            "SELECT id,request_sha256 FROM public.governance_command_receipts "
            "WHERE command_name='governance.bootstrap.close' AND status='succeeded'"
        ).fetchone()
        anchor_id = connection.execute(
            "SELECT anchor_id FROM public.school_database_anchor"
        ).fetchone()[0]
        assignment_id = connection.execute("SELECT id FROM public.staff_assignments").fetchone()[0]
        assert initial[3]["target_version"] >= 2
        base_version = initial[3]["target_version"]
        password = secrets.token_urlsafe(32)
        connection.execute(
            sql.SQL("ALTER ROLE campus_ai_governance_writer LOGIN PASSWORD {}").format(
                sql.Literal(password)
            )
        )

    database = roles.admin_connection_parameters["dbname"]
    reader = configuration_payload()["reader"]
    reader.update(
        database_host=roles.reader_connection_parameters["host"],
        database_port=roles.reader_connection_parameters["port"],
        database_name=database,
        database_password=roles.reader_connection_parameters["password"],
        schema_contract="school-facts-live-v57",
        expected_school_id=school,
        expected_anchor_id=anchor_id,
        expected_system_identifier=roles.disposable_cluster_system_identifier,
    )
    closure = dict(
        school_id=school,
        anchor_id=anchor_id,
        system_identifier=roles.disposable_cluster_system_identifier,
        database_name=database,
        initialization_request_id=initial[1],
        initialization_close_receipt_id=initial[0],
        initialization_close_receipt_sha256=sha256(
            json.dumps(initial, default=str).encode()
        ).hexdigest(),
        governance_close_receipt_id=close[0],
        governance_close_receipt_sha256=close[1],
    )
    now = datetime.now(UTC) - timedelta(seconds=1)
    foundation = api.EngineeringManifest(
        manifest_version="live_governance_v1",
        school_id=school,
        actor_reference="synthetic:deferred-deployment-operator",
        approval_reference="synthetic:reviewed-deferred-f2",
        valid_from=now,
        valid_to=now + timedelta(minutes=15),
        grant_epoch=adapter.manifest.grant_valid_from,
        grant_valid_to=adapter.manifest.grant_valid_to,
        approved_duty_codes=frozenset(
            {
                "f2_organization_maintainer",
                "f2_organization_reader",
            }
        ),
        approved_assignment_ids=frozenset({assignment_id}),
        commands=tuple(
            dict(
                command="duty",
                key=f"{kind}-{purpose}-duty",
                duty_code=f"f2_{kind}_{purpose}",
                display_name="Synthetic exact F2 duty",
                effective_from=now,
            )
            for kind in ("organization",)
            for purpose in ("maintainer", "reader")
        )
        + tuple(
            dict(
                command="scope",
                key=f"{kind}-{purpose}-scope",
                family="f2",
                fact_kind=kind,
                action="maintain" if purpose == "maintainer" else "read",
                coverage="school",
                object_keys=(),
                creation_keys=(),
                field_names=(),
                f2_object_level_acknowledged=True,
                effective_from=now,
            )
            for kind in ("organization",)
            for purpose in ("maintainer", "reader")
        ),
    )

    def configuration(manifest, label):
        path = tmp_path / f"deferred-{label}-manifest.json"
        path.write_text(manifest.model_dump_json(), encoding="utf-8")
        digest = sha256(path.read_bytes()).hexdigest()
        evidence = api.DeploymentEvidence(
            purpose="school-facts-live-governance",
            approval_reference=manifest.approval_reference,
            issuer_reference="synthetic:reviewer",
            issuer_qualification_reference="synthetic:qualification",
            operator_reference=manifest.actor_reference,
            source_authority_reference="synthetic:fixture",
            approved_at=manifest.valid_from,
            expires_at=manifest.valid_to,
            closure=api.ClosureEvidence(**closure),
            approved_manifest_sha256=digest,
            recipient_assignment_ids=manifest.approved_assignment_ids,
        )
        epath = tmp_path / f"deferred-{label}-evidence.json"
        epath.write_text(evidence.model_dump_json(), encoding="utf-8")
        return api.EngineeringPrivateConfiguration(
            approved_manifest_sha256=digest,
            manifest_path=str(path),
            actor_reference=manifest.actor_reference,
            credential="synthetic-deferred-test-credential",
            database_host=reader["database_host"],
            database_port=reader["database_port"],
            database_name=database,
            database_user="campus_ai_governance_writer",
            database_password=password,
            expected_anchor_id=anchor_id,
            expected_school_id=school,
            expected_system_identifier=roles.disposable_cluster_system_identifier,
            reader=reader,
            approval_reference=manifest.approval_reference,
            grant_epoch=manifest.grant_epoch,
            expected_schema_contract="school-facts-live-v57",
            evidence_path=str(epath),
            approved_evidence_sha256=sha256(epath.read_bytes()).hexdigest(),
        )

    resources = []
    try:
        config = configuration(foundation, "foundation")
        service, engines = api.compose(config, foundation, approval_is_current=lambda: True)
        resources.extend(engines)
        credential = config.credential.get_secret_value()
        scope_ids = {}
        for approved, command in zip(foundation.commands, foundation.materialize(), strict=True):
            prepared = service.prepare(command, credential=credential)
            confirmed = service.confirm(prepared.confirmation_token, credential=credential)
            if approved.command == "scope":
                scope_ids[approved.key] = confirmed.target_id
        bindings = [
            api.GrantBinding(
                key=f"{kind}-{purpose}-grant",
                assignment_id=assignment_id,
                duty_key=f"{kind}-{purpose}-duty",
                scope_key=f"{kind}-{purpose}-scope",
            )
            for kind in ("organization",)
            for purpose in ("maintainer", "reader")
        ]
        grants = api.build_grants(
            foundation,
            bindings,
            grant_valid_from=now,
            execution_valid_to=foundation.valid_to,
            service=service,
            credential=credential,
        )
        gconfig = configuration(grants, "grants")
        gservice, engines = api.compose(gconfig, grants, approval_is_current=lambda: True)
        resources.extend(engines)
        for command in grants.materialize():
            prepared = gservice.prepare(command, credential=credential)
            confirmed = gservice.confirm(prepared.confirmation_token, credential=credential)
            assert gservice.receipt(command, credential=credential).target_id == confirmed.target_id

        runtime = Settings.model_validate(
            {
                **adapter.config.runtime.model_dump(),
                "postgres_schema_contract": "school-facts-live-v57",
            }
        )
        runtime_engine = build_engine(runtime)
        resources.append(runtime_engine)
        identity = build_school_fact_dingtalk_identity_service(
            settings=runtime,
            session_factory=build_session_factory(runtime_engine),
            redis=adapter.redis,
            proof_signer=ContextProofSigner.from_settings(runtime),
            can_maintain=lambda actor: actor.assignment_id == assignment_id,
        )
        token = adapter.identity.token.get_secret_value()
        view = identity.session_view(token)
        assert view.status == "signed_in" and len(view.contexts) == 1
        if view.selected_context_ref is None:
            identity.select_context(token, view.contexts[0].ref)
        current = identity.resolve_request(token)
        assert current.actor.assignment_id == assignment_id
        reader_settings = SchoolFactReaderSettings.model_validate(
            {**adapter.config.reader.model_dump(), "schema_contract": "school-facts-live-v57"}
        )
        writer_settings = SchoolFactWriterSettings.model_validate(
            {**adapter.config.writer.model_dump(), "schema_contract": "school-facts-live-v57"}
        )
        reader_engine = build_school_fact_reader_engine(reader_settings)
        writer_engine = build_school_fact_writer_engine(writer_settings)
        resources.extend((reader_engine, writer_engine))
        directory = create_directory_read_components(
            sessions=sessionmaker(bind=reader_engine, expire_on_commit=False, autoflush=False),
            settings=reader_settings,
            configuration=DirectoryConfiguration(
                approval_reference="synthetic:reviewed-deferred-f2",
                scope_bindings=tuple(
                    DirectoryScopeBinding(
                        assignment_id=assignment_id,
                        operation_code=(
                            f"school_facts.f2.{kind}."
                            + ("maintain" if purpose == "maintainer" else "read")
                        ),
                        scope_id=scope_ids[f"{kind}-{purpose}-scope"],
                    )
                    for kind in ("organization",)
                    for purpose in ("maintainer", "reader")
                ),
                source_catalog={},
            ),
            legacy_scope_id=uuid4(),
            reference_key=reader_settings.cursor_hmac_key.get_secret_value().encode(),
        )
        reader_sessions = sessionmaker(bind=reader_engine, expire_on_commit=False, autoflush=False)
        family_configuration = FamilyConfiguration(
            enabled=False,
            approval_reference="synthetic:deferred-no-f3-authority",
            scope_bindings=(),
        )
        family = create_family_authority_components(
            session_factory=reader_sessions, configuration=family_configuration
        )
        family_final = FamilyFinalAuthorizationGuard(
            family.change_set_guard, enabled=family_configuration.enabled
        )
        final_guard = FamilyWriterDispatch(
            family=family_final,
            directory=directory.guard,
            configuration=family_configuration,
        )
        partition = PackageFactPartition(
            directory_configuration=DirectoryConfiguration(
                approval_reference="synthetic:reviewed-deferred-f2",
                scope_bindings=tuple(
                    DirectoryScopeBinding(
                        assignment_id=assignment_id,
                        operation_code=f"school_facts.f2.{kind}.maintain",
                        scope_id=scope_ids[f"{kind}-maintainer-scope"],
                    )
                    for kind in ("organization",)
                ),
            ),
            family_configuration=family_configuration,
            education_configuration=None,
            existing_quality_kind=PackageExistingQualityKind(
                f2_guard=directory.guard, sessions=reader_sessions
            ).target_kind,
        )
        package_guard = PackageWriterAuthorization(
            partition,
            {"directory": directory.guard, "family": family_final},
            references=PackageReferenceAuthorization(
                partition,
                directory_guard=directory.guard,
                family_authority=family,
                education_authority=None,
                residence_authority=None,
            ),
        )
        own_result = PartitionedOwnResultAuthorizer(
            dispatch=final_guard,
            directory=directory.own_result_authorizer,
            family=F3OwnResultAuthorizer(
                thin=family.thin,
                configuration=family_configuration,
                clock=lambda: datetime.now(UTC),
            ),
        )

        def change_set(base, expected, label):
            return CanonicalFactChangeSetV1.model_validate(
                {
                    "base_version": base,
                    "source_sha256": ("a" if expected == 0 else "b") * 64,
                    "changes": (
                        {
                            "operation": "upsert",
                            "fact_kind": "organization",
                            "source_key": "organization:future",
                            "expected_version": expected,
                            "effective_at": datetime.now(UTC),
                            "origin": {"sheet_code": "scope", "row_number": 1},
                            "content": {
                                "kind": "department",
                                "display_name": label,
                                "display_path": label,
                                "status": "active",
                            },
                        },
                    ),
                }
            )

        changes = change_set(base_version, 0, "Synthetic future organization")
        context = compose_command_context(current.actor, uuid4(), "deferred-f2-first-publication")
        kernel = SqlAlchemySchoolFactWriterExecutorKernel(
            engine=writer_engine,
            settings=writer_settings,
            final_authorization_guard=final_guard,
            package_final_authorization_guard=package_guard,
            family_actor_validator=family.actor_validator,
            own_result_authorizer=own_result,
            own_terminal_authorizer=directory.own_terminal_authorizer,
        )
        report = kernel.preflight(
            school_id=school,
            change_set=changes,
            context=context,
            session_binding=current.session_binding,
        )
        assert report.is_clean, report.issues
        prepared = kernel.prepare(
            change_set=changes,
            context=context,
            session_binding=current.session_binding,
            provenance=SchoolFactChangeProvenance(
                channel="admin",
                raw_source_sha256=changes.source_sha256,
                template_version="pytest_v1",
                mapping_version="pytest_v1",
                mapping_config_sha256="c" * 64,
                source_size_bytes=None,
                worksheet_names=(),
            ),
        )
        confirmed = kernel.confirm(
            credential=prepared.credential,
            change_set=changes,
            context=context,
            session_binding=current.session_binding,
        )
        assert confirmed.changed_count == 1 and not confirmed.replayed
        original = kernel.confirm(
            credential=prepared.credential,
            change_set=changes,
            context=context,
            session_binding=current.session_binding,
        )
        assert original.batch_id == confirmed.batch_id and original.replayed
        fresh = identity.resolve_request(token)
        lookup = kernel.result_by_digest(
            school_id=school,
            source_sha256=changes.source_sha256,
            plan_sha256=report.plan_sha256,
            context=compose_command_context(
                fresh.actor, context.request_id, context.idempotency_key
            ),
            session_binding=fresh.session_binding,
        )
        assert lookup.found and lookup.result is not None
        assert lookup.result.batch_id == confirmed.batch_id
        for version, label in (
            (1, "Synthetic future organization"),
            (2, "Synthetic updated organization"),
        ):
            fresh = identity.resolve_request(token)
            reference = directory.query.reference_for(
                actor=fresh.actor, fact_kind="organization", source_key="organization:future"
            )
            assert reference.startswith("schoolv1:f2:organization:o:")
            result = directory.query.query(
                actor=fresh.actor,
                fact_kind="organization",
                reference=reference,
                factual_at=datetime.now(UTC),
            )
            assert result.status == "known" and result.records[0].content["display_name"] == label
            if version == 1:
                update = change_set(confirmed.target_version, 1, "Synthetic updated organization")
                update_context = compose_command_context(
                    fresh.actor, uuid4(), "school-scope-update"
                )
                updated = kernel.prepare(
                    change_set=update,
                    context=update_context,
                    session_binding=fresh.session_binding,
                    provenance=SchoolFactChangeProvenance(
                        channel="admin",
                        raw_source_sha256=update.source_sha256,
                        template_version="pytest_v1",
                        mapping_version="pytest_v1",
                        mapping_config_sha256="c" * 64,
                        source_size_bytes=None,
                        worksheet_names=(),
                    ),
                )
                result = kernel.confirm(
                    credential=updated.credential,
                    change_set=update,
                    context=update_context,
                    session_binding=fresh.session_binding,
                )
                assert result.changed_count == 1
        fresh = identity.resolve_request(token)
        original_lookup = kernel.result_by_digest(
            school_id=school,
            source_sha256=changes.source_sha256,
            plan_sha256=report.plan_sha256,
            context=compose_command_context(
                fresh.actor, context.request_id, context.idempotency_key
            ),
            session_binding=fresh.session_binding,
        )
        assert original_lookup.found and original_lookup.result.batch_id == confirmed.batch_id
        from app.composition.school_fact_directory_services import F2ScopeOwnerLookup
        from app.platform.governance.application import GovernanceWriter
        from app.platform.governance.contracts import RevokeGrantCommand
        from app.platform.school_facts.application import SchoolFactChangeRejected
        from app.platform.school_facts.f2_policy import F2ScopeResolver

        with roles.admin_connection() as connection:
            grant_ids = connection.execute(
                "SELECT g.id FROM public.governance_grants g "
                "JOIN public.governance_duties d ON d.id=g.duty_id "
                "WHERE d.duty_code IN ('f2_organization_reader','f2_organization_maintainer')"
            ).fetchall()
        targets = {row[0] for row in grant_ids}

        class SyntheticRevokeAuthorizer:
            def authorize(self, request):
                return (
                    request.school_id == school
                    and request.command_name == "governance.grant.revoke"
                    and request.target_id in targets
                )

        revoker = GovernanceWriter(
            session_factory=gservice._writer._session_factory,
            authorizer=SyntheticRevokeAuthorizer(),
            scope_owner_resolvers={
                "school_facts.f2": F2ScopeResolver(lookup=F2ScopeOwnerLookup(reader_sessions))
            },
            clock=lambda: datetime.now(UTC),
            dependency_port=gservice._writer._dependency_port,
        )
        for grant_id in targets:
            revoker.revoke_grant(
                RevokeGrantCommand(
                    school_id=school,
                    grant_id=grant_id,
                    expected_version=1,
                    effective_at=datetime.now(UTC),
                    actor=adapter._actor("synthetic-school-scope-revoke"),
                    idempotency_key=str(uuid4()),
                )
            )
        fresh = identity.resolve_request(token)
        with pytest.raises((PermissionError, SchoolFactChangeRejected, ValueError)):
            directory.query.query(
                actor=fresh.actor,
                fact_kind="organization",
                reference=reference,
                factual_at=datetime.now(UTC),
            )
        with pytest.raises((PermissionError, SchoolFactChangeRejected, ValueError)):
            directory.guard.require_current(fresh.actor, "organization", purpose="maintain")
    finally:
        for resource in resources:
            resource.dispose()
        with roles.admin_connection() as connection:
            connection.execute("ALTER ROLE campus_ai_governance_writer NOLOGIN")
