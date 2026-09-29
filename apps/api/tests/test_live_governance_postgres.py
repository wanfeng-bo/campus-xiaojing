"""Synthetic owned PG16 validation: closed schema22 initialization through schema56 live batch.

Reuses the existing production initialization exercise; no original database,
real qualification, DingTalk network exchange, model call or deployment is involved.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import replace
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


def test_live_batch_real_writer_after_dual_closure(identity_template, monkeypatch, tmp_path):  # noqa: F811
    __tracebackhide__ = True
    probes = []

    def required_upgraded(roles):
        _exercise_live_batch(roles, tmp_path)
        probes.append(True)

    initialization.test_formal_initialization_closes_and_preserves_upgrade(
        identity_template, monkeypatch, tmp_path, on_upgraded=required_upgraded
    )
    assert probes == [True]

    # The same node also exercises the separately owned deferred database. Capture
    # its real initialization adapter so the later Writer uses its DingTalk session.
    adapters = []
    real_adapter = initialization.LiveFirstMaintainer

    def capture_adapter(*args, **kwargs):
        adapter = real_adapter(*args, **kwargs)
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(initialization, "LiveFirstMaintainer", capture_adapter)

    def deferred_upgraded(roles):
        assert len(adapters) == 1
        _exercise_deferred_f2_writer(roles, adapters[0], tmp_path)
        probes.append("deferred")

    initialization.test_formal_initialization_closes_and_preserves_upgrade(
        identity_template,
        monkeypatch,
        tmp_path,
        publication_policy="deferred",
        on_upgraded=deferred_upgraded,
    )
    assert probes == [True, "deferred"]


def _exercise_live_batch(roles, tmp_path):
    __tracebackhide__ = True
    roles.verify_disposable_cluster()
    with roles.admin_connection() as connection:
        assert connection.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            "20260924_56",
        )
        anchor = connection.execute(
            "SELECT school_id,anchor_id FROM public.school_database_anchor"
        ).fetchone()
        school, anchor_id = anchor
        initialization_row = connection.execute(
            "SELECT id,request_id,state,result_payload,closed_at "
            "FROM public.school_fact_initializations"
        ).fetchone()
        assert initialization_row[2] == "closed"
        assert connection.execute(
            "SELECT state FROM public.governance_bootstrap_states"
        ).fetchone() == ("closed",)
        close_row = connection.execute(
            "SELECT id,request_sha256 FROM public.governance_command_receipts "
            "WHERE command_name='governance.bootstrap.close' AND status='succeeded'"
        ).fetchone()
        recipient = connection.execute(
            "SELECT id FROM public.staff_assignments LIMIT 1"
        ).fetchone()[0]
        person_key = connection.execute(
            "SELECT source_key FROM public.school_people LIMIT 1"
        ).fetchone()[0]
        password = secrets.token_urlsafe(32)
        connection.execute(
            sql.SQL("ALTER ROLE campus_ai_governance_writer LOGIN PASSWORD {}").format(
                sql.Literal(password)
            )
        )
    now = datetime.now(UTC) - timedelta(seconds=1)
    m = api.EngineeringManifest(
        manifest_version="live_governance_v1",
        school_id=school,
        actor_reference="synthetic:independent-deployment-operator",
        approval_reference="synthetic:reviewed-live-batch",
        valid_from=now,
        valid_to=now + timedelta(minutes=15),
        grant_epoch=now,
        grant_valid_to=now + timedelta(days=90),
        approved_duty_codes=frozenset({"f2_person_reader"}),
        approved_assignment_ids=frozenset({recipient}),
        commands=(
            dict(
                command="duty",
                key="person-reader",
                duty_code="f2_person_reader",
                display_name="Synthetic person reader",
                effective_from=now,
            ),
            dict(
                command="scope",
                key="person-scope",
                family="f2",
                fact_kind="person",
                action="read",
                object_keys=(person_key,),
                field_names=(),
                f2_object_level_acknowledged=True,
                effective_from=now,
            ),
        ),
    )
    database = roles.admin_connection_parameters["dbname"]
    reader = configuration_payload()["reader"]
    reader.update(
        database_host=roles.reader_connection_parameters["host"],
        database_port=roles.reader_connection_parameters["port"],
        database_name=database,
        database_password=roles.reader_connection_parameters["password"],
        schema_contract="school-facts-f7-v56",
        expected_school_id=school,
        expected_anchor_id=anchor_id,
        expected_system_identifier=roles.disposable_cluster_system_identifier,
    )
    closure = dict(
        school_id=school,
        anchor_id=anchor_id,
        system_identifier=roles.disposable_cluster_system_identifier,
        database_name=database,
        initialization_request_id=initialization_row[1],
        initialization_close_receipt_id=initialization_row[0],
        initialization_close_receipt_sha256=sha256(
            json.dumps(initialization_row, default=str).encode()
        ).hexdigest(),
        governance_close_receipt_id=close_row[0],
        governance_close_receipt_sha256=close_row[1],
    )

    def configuration(manifest, label):
        path = tmp_path / (label + "-manifest.json")
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
        epath = tmp_path / (label + "-evidence.json")
        epath.write_text(evidence.model_dump_json(), encoding="utf-8")
        return api.EngineeringPrivateConfiguration(
            approved_manifest_sha256=digest,
            manifest_path=str(path),
            actor_reference=manifest.actor_reference,
            credential="synthetic-independent-test-credential",
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
            expected_schema_contract="school-facts-f7-v56",
            evidence_path=str(epath),
            approved_evidence_sha256=sha256(epath.read_bytes()).hexdigest(),
        )

    gate = [True]
    resources = []
    try:
        config = configuration(m, "foundation")
        service, engines = api.compose(config, m, approval_is_current=lambda: gate[0])
        resources.extend(engines)
        credential = config.credential.get_secret_value()
        receipts = []
        for command in m.materialize():
            prepared = service.prepare(command, credential=credential)
            result = service.confirm(prepared.confirmation_token, credential=credential)
            assert service.receipt(command, credential=credential).target_id == result.target_id
            assert service.confirm(prepared.confirmation_token, credential=credential).replayed
            receipts.append(result)
        bound = api.GrantBinding(
            key="person-grant",
            assignment_id=recipient,
            duty_key="person-reader",
            scope_key="person-scope",
        )
        grants = api.build_grants(
            m,
            [bound],
            grant_valid_from=now,
            execution_valid_to=m.valid_to,
            service=service,
            credential=credential,
        )
        gconfig = configuration(grants, "grant")
        gservice, engines = api.compose(gconfig, grants, approval_is_current=lambda: gate[0])
        resources.extend(engines)
        command = grants.materialize()[0]
        with pytest.raises(RuntimeError):
            gservice.prepare(replace(command, assignment_id=uuid4()), credential=credential)
        invalid = m.model_dump()
        invalid["commands"] = (
            dict(
                command="scope",
                key="invalid-fields",
                family="f3",
                fact_kind="person",
                action="read",
                object_keys=(person_key,),
                field_names=("invented_field",),
                effective_from=now,
            ),
        )
        invalid["approved_duty_codes"] = frozenset({"f3_person_reader"})
        with pytest.raises(ValueError):
            api.EngineeringManifest.model_validate(invalid)
        prepared = gservice.prepare(command, credential=credential)
        receipt = gservice.confirm(prepared.confirmation_token, credential=credential)
        gate[0] = False
        with pytest.raises(RuntimeError):
            gservice.confirm(prepared.confirmation_token, credential=credential)
        assert gservice.receipt(command, credential=credential).target_id == receipt.target_id
        with roles.admin_connection() as connection:
            assert connection.execute(
                "SELECT planned_valid_to-valid_from FROM public.governance_grant_versions "
                "WHERE grant_id=%s",
                (receipt.target_id,),
            ).fetchone() == (timedelta(days=90),)
            assert connection.execute(
                "SELECT count(*) FROM public.governance_grants WHERE id=%s", (receipt.target_id,)
            ).fetchone() == (1,)
    finally:
        for engine in resources:
            engine.dispose()
        with roles.admin_connection() as connection:
            connection.execute("ALTER ROLE campus_ai_governance_writer NOLOGIN")


def _exercise_deferred_f2_writer(roles, adapter, tmp_path):
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
    from tests.test_f1_initial_publication import change_set as publication_facts

    roles.verify_disposable_cluster()
    school = adapter.manifest.school_id
    with roles.admin_connection() as connection:
        assert connection.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            "20260924_56",
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
        schema_contract="school-facts-f7-v56",
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
        grant_epoch=now,
        grant_valid_to=now + timedelta(days=90),
        approved_duty_codes=frozenset(
            {
                "f2_location_maintainer",
                "f2_official_contact_maintainer",
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
            for kind in ("location", "official_contact")
            for purpose in ("maintainer",)
        )
        + tuple(
            dict(
                command="scope",
                key=f"{kind}-{purpose}-scope",
                family="f2",
                fact_kind=kind,
                action="maintain" if purpose == "maintainer" else "read",
                object_keys=(f"f1:{'campus' if kind == 'location' else 'contact'}",)
                if purpose == "reader"
                else (),
                creation_keys=(f"f1:{'campus' if kind == 'location' else 'contact'}",)
                if purpose == "maintainer"
                else (),
                field_names=(),
                f2_object_level_acknowledged=True,
                effective_from=now,
            )
            for kind in ("location", "official_contact")
            for purpose in ("maintainer",)
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
            expected_schema_contract="school-facts-f7-v56",
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
            for kind in ("location", "official_contact")
            for purpose in ("maintainer",)
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
                "postgres_schema_contract": "school-facts-f7-v56",
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
            {**adapter.config.reader.model_dump(), "schema_contract": "school-facts-f7-v56"}
        )
        writer_settings = SchoolFactWriterSettings.model_validate(
            {**adapter.config.writer.model_dump(), "schema_contract": "school-facts-f7-v56"}
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
                        operation_code=f"school_facts.f2.{kind}.maintain",
                        scope_id=scope_ids[f"{kind}-{purpose}-scope"],
                    )
                    for kind in ("location", "official_contact")
                    for purpose in ("maintainer",)
                ),
                source_catalog={"location": ("f1:campus",), "official_contact": ("f1:contact",)},
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
                    for kind in ("location", "official_contact")
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
        changes = publication_facts(base_version=base_version)
        changes = CanonicalFactChangeSetV1.model_validate(
            changes.model_copy(
                update={
                    "changes": tuple(
                        change.model_copy(update={"effective_at": datetime.now(UTC)})
                        for change in changes.changes
                    )
                }
            )
        )
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
        assert confirmed.changed_count == 2 and not confirmed.replayed
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
        # Current-object Reader scopes can only be reviewed and signed after the
        # confirmed formal publication makes those exact objects real.
        read_now = foundation.grant_epoch
        read_foundation = api.EngineeringManifest(
            manifest_version="live_governance_v1",
            school_id=school,
            actor_reference="synthetic:deferred-deployment-operator",
            approval_reference="synthetic:reviewed-deferred-f2-read",
            valid_from=read_now,
            valid_to=read_now + timedelta(minutes=15),
            grant_epoch=read_now,
            grant_valid_to=foundation.grant_valid_to,
            approved_duty_codes=frozenset(
                {
                    "f2_location_reader",
                    "f2_official_contact_reader",
                }
            ),
            approved_assignment_ids=frozenset({assignment_id}),
            commands=tuple(
                dict(
                    command="duty",
                    key=f"{kind}-reader-duty",
                    duty_code=f"f2_{kind}_reader",
                    display_name="Synthetic exact F2 reader",
                    effective_from=read_now,
                )
                for kind in ("location", "official_contact")
            )
            + tuple(
                dict(
                    command="scope",
                    key=f"{kind}-reader-scope",
                    family="f2",
                    fact_kind=kind,
                    action="read",
                    object_keys=(f"f1:{'campus' if kind == 'location' else 'contact'}",),
                    field_names=(),
                    f2_object_level_acknowledged=True,
                    effective_from=read_now,
                )
                for kind in ("location", "official_contact")
            ),
        )
        read_config = configuration(read_foundation, "read-foundation")
        read_service, engines = api.compose(
            read_config, read_foundation, approval_is_current=lambda: True
        )
        resources.extend(engines)
        for approved, command in zip(
            read_foundation.commands, read_foundation.materialize(), strict=True
        ):
            prepared = read_service.prepare(command, credential=credential)
            created = read_service.confirm(prepared.confirmation_token, credential=credential)
            if approved.command == "scope":
                scope_ids[approved.key] = created.target_id
        read_grants = api.build_grants(
            read_foundation,
            [
                api.GrantBinding(
                    key=f"{kind}-reader-grant",
                    assignment_id=assignment_id,
                    duty_key=f"{kind}-reader-duty",
                    scope_key=f"{kind}-reader-scope",
                )
                for kind in ("location", "official_contact")
            ],
            grant_valid_from=read_now,
            execution_valid_to=read_foundation.valid_to,
            service=read_service,
            credential=credential,
        )
        read_grant_config = configuration(read_grants, "read-grants")
        read_grant_service, engines = api.compose(
            read_grant_config, read_grants, approval_is_current=lambda: True
        )
        resources.extend(engines)
        for command in read_grants.materialize():
            prepared = read_grant_service.prepare(command, credential=credential)
            read_grant_service.confirm(prepared.confirmation_token, credential=credential)
        read_directory = create_directory_read_components(
            sessions=sessionmaker(bind=reader_engine, expire_on_commit=False, autoflush=False),
            settings=reader_settings,
            configuration=DirectoryConfiguration(
                approval_reference="synthetic:reviewed-deferred-f2-read",
                scope_bindings=tuple(
                    DirectoryScopeBinding(
                        assignment_id=assignment_id,
                        operation_code=f"school_facts.f2.{kind}.read",
                        scope_id=scope_ids[f"{kind}-reader-scope"],
                    )
                    for kind in ("location", "official_contact")
                ),
                source_catalog={"location": ("f1:campus",), "official_contact": ("f1:contact",)},
            ),
            legacy_scope_id=uuid4(),
            reference_key=reader_settings.cursor_hmac_key.get_secret_value().encode(),
        )
        for kind, key in (("location", "f1:campus"), ("official_contact", "f1:contact")):
            fresh = identity.resolve_request(token)
            reference = read_directory.query.reference_for(
                actor=fresh.actor, fact_kind=kind, source_key=key
            )
            result = read_directory.query.query(
                actor=fresh.actor,
                fact_kind=kind,
                reference=reference,
                factual_at=datetime.now(UTC),
            )
            assert result.status == "known" and len(result.records) == 1
            record = result.records[0]
            assert record.source_kind == "formal_publication"
            assert record.publication_ref == f"publication:{confirmed.batch_id}"
            assert record.published_at is not None
            assert record.content["display_name" if kind == "location" else "contact_value"] == (
                "合成主校区" if kind == "location" else "0571-00000000"
            )
    finally:
        for resource in resources:
            resource.dispose()
        with roles.admin_connection() as connection:
            connection.execute("ALTER ROLE campus_ai_governance_writer NOLOGIN")
