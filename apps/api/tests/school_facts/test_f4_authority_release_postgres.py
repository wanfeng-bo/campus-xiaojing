"""Actual F4 Reader/Thin/Governance SQL release checks in an owned DB only.

Governance command authorization and test Staff credential issuance are explicit
fixture prerequisites, not original-instance operator authorization. Grant create
and revoke use the real Governance Writer. All owner fact changes use the sole
canonical Writer. No schema, role, or original-instance operation is performed.
"""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_education_authority import (
    EducationAuthorityDenied,
    EducationScopeOwnerLookup,
    create_education_authority,
)
from app.composition.school_fact_education_reader import (
    EducationReadProof,
    EducationRepository,
    SqlEducationProjection,
)
from app.composition.school_fact_education_settings import (
    EducationConfiguration,
    EducationScopeBinding,
)
from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
from app.composition.school_fact_read_connection import F2ConnectionReadExecutor
from app.domains.students.education_contracts import EDUCATION_CONTENT_FIELDS
from app.platform.governance.application import GovernanceWriter
from app.platform.governance.contracts import (
    CreateDutyCommand,
    CreateGrantCommand,
    CreateScopeCommand,
    GovernanceActorContext,
    RevokeGrantCommand,
)
from app.platform.school_facts.f4_policy import (
    F4_SCOPE_OWNER,
    F4_SCOPE_SCHEMA,
    F4FactScope,
    F4ScopeResolver,
)
from tests.governance.test_writer_lifecycle import _document_sha256, _RecordingAuthorizer
from tests.school_facts.test_f4_writer_postgres import apply
from tests.school_facts.test_f4_writer_postgres import writer as writer

pytestmark = pytest.mark.skipif(
    not os.environ.get("F4_WRITER_PG"), reason="explicit owned F4 PG target required"
)


def test_real_f4_proof_rejects_grant_revoke_and_mid_batch_root_change_then_releases_connections(
    writer, monkeypatch
):
    marker = "f4-release:" + uuid4().hex
    engines = writer[0]
    school = writer[1].school_id

    def now():
        return datetime.now(UTC)

    at = now() - timedelta(seconds=2)
    keys = {kind: marker + ":" + kind for kind in ("education_class", "education_stage")}
    apply(
        writer,
        [
            dict(
                fact_kind=kind,
                source_key=key,
                operation="upsert",
                expected_version=0,
                effective_at=at,
                content={"display_name": "Owned release proof fixture"},
            )
            for kind, key in keys.items()
        ],
    )
    owner = sessionmaker(engines["migration"], expire_on_commit=False)
    readers = sessionmaker(engines["reader"], expire_on_commit=False)
    # The retained executor actor is a credential fixture without formal owner
    # lineage. Publish separate real owner roots for the authorization under test.
    roots = {
        "person": {
            "display_name": "Owned release staff",
            "person_kind": "staff",
            "lifecycle_status": "active",
        },
        "organization": {
            "kind": "department",
            "display_name": "Owned release unit",
            "display_path": "Owned release unit",
            "status": "active",
        },
        "assignment": {
            "person_source_key": marker + ":person",
            "organization_source_key": marker + ":organization",
            "position_title": "Owned verifier",
            "semantic_role_code": "teacher",
            "assignment_kind": "substantive",
            "appointment_source": "test-only",
            "approval_reference": marker,
            "status": "confirmed",
        },
    }
    apply(
        writer,
        [
            dict(
                fact_kind=kind,
                source_key=marker + ":" + kind,
                operation="upsert",
                expected_version=0,
                effective_at=at,
                content=content,
            )
            for kind, content in roots.items()
        ],
    )
    with owner() as session:
        staff = session.execute(
            text(
                "SELECT id,person_id,version FROM staff_assignments "
                "WHERE school_id=:school AND source_key=:key"
            ),
            {"school": school, "key": marker + ":assignment"},
        ).one()
    target_assignment = staff.id
    governance = GovernanceWriter(
        session_factory=owner,
        authorizer=_RecordingAuthorizer(),
        clock=now,
        dependency_port=ReadOnlyGovernanceDependencies(readers),
        scope_owner_resolvers={
            F4_SCOPE_OWNER: F4ScopeResolver(
                lookup_scope=EducationScopeOwnerLookup(readers).scope_exists
            )
        },
    )

    def command_actor():
        return GovernanceActorContext(
            school_id=school,
            actor_kind="assignment",
            actor_reference="test-only:" + marker,
            actor_assignment_id=target_assignment,
            actor_assignment_version=writer[1].assignment_version,
            request_id=uuid4(),
            reason="Owned test authorization fixture; not original approval",
        )

    duties = {}
    scopes = {}
    grants = {}
    bindings = []
    for kind, key in keys.items():
        code = f"school_facts.f4.{kind}.read"
        duty_code = f"f4_{kind}_reader"
        with owner() as s:
            duty = s.execute(
                text(
                    "SELECT id,current_version AS version FROM governance_duties "
                    "WHERE school_id=:s AND duty_code=:code"
                ),
                {"s": school, "code": duty_code},
            ).one_or_none()
        if duty is None:
            made = governance.create_duty(
                CreateDutyCommand(
                    school,
                    duty_code,
                    "Owned F4 release duty",
                    "Test-only prerequisite",
                    at,
                    command_actor(),
                    marker + ":duty:" + kind,
                )
            )
            duty_id, duty_version = made.target_id, made.target_version
        else:
            duty_id, duty_version = duty.id, duty.version
        duties[kind] = (duty_id, duty_version)
        scope = F4FactScope(
            school,
            kind,
            object_keys=(key,),
            field_names=tuple(sorted(EDUCATION_CONTENT_FIELDS[kind])),
        )
        made = governance.create_scope(
            CreateScopeCommand(
                school,
                marker + ":scope:" + kind,
                F4_SCOPE_OWNER,
                F4_SCOPE_SCHEMA,
                1,
                scope.document,
                _document_sha256(scope.document),
                at,
                command_actor(),
                marker + ":scope:" + kind,
            )
        )
        scopes[kind] = made
        bindings.append(
            EducationScopeBinding(
                assignment_id=target_assignment, operation_code=code, scope_id=made.target_id
            )
        )

    def grant(kind, suffix):
        duty_id, duty_version = duties[kind]
        scope = scopes[kind]
        return governance.create_grant(
            CreateGrantCommand(
                school,
                target_assignment,
                duty_id,
                duty_version,
                scope.target_id,
                scope.target_version,
                "test-only.f4-release",
                marker + ":basis",
                at,
                at,
                now() + timedelta(hours=1),
                command_actor(),
                marker + ":grant:" + kind + suffix,
            )
        )

    for kind in keys:
        grants[kind] = grant(kind, "initial")
    authority = create_education_authority(
        sessions=readers,
        configuration=EducationConfiguration(
            approval_reference=marker, scope_bindings=tuple(bindings)
        ),
    )
    repository = EducationRepository(
        authority=authority,
        reference_key=b"owned-release-fixture-key-32-bytes",
        projection=SqlEducationProjection(
            F2ConnectionReadExecutor(readers, authority.connection_authority),
            current_sessions=authority.connection_authority.current_sessions,
        ),
    )
    held = [0]

    def checkout(*_):
        held[0] += 1

    def checkin(*_):
        held[0] -= 1

    event.listen(engines["reader"], "checkout", checkout)
    event.listen(engines["reader"], "checkin", checkin)

    def current():
        from app.platform.contracts import _issue_staff_actor_context

        with owner() as session:
            epoch = session.scalar(
                text(
                    "SELECT epoch FROM governance_assignment_epochs "
                    "WHERE school_id=:school AND assignment_id=:assignment"
                ),
                {"school": school, "assignment": target_assignment},
            )
        # Credential issuance is the only test stub. Every authority and scope
        # decision below reads real published roots, grants, epochs and scopes.
        who = _issue_staff_actor_context(
            account_user_id=writer[1].account_user_id,
            school_id=school,
            person_id=staff.person_id,
            assignment_id=target_assignment,
            assignment_version=staff.version,
            authorization_version=epoch,
        )
        return who, writer[2], writer[3], writer[4]

    def read_proof(actor):
        reads = tuple(
            repository.read_records(actor, kind, fields=("display_name",), source_keys=(key,))
            for kind, key in keys.items()
        )
        assert all(
            r.records and r.records[0]["source_key"] == keys[k]
            for r, k in zip(reads, keys, strict=True)
        )
        assert held[0] == 0
        return EducationReadProof.combine(tuple(r.proof for r in reads))

    try:
        actor, identity, context, service = current()
        proof = read_proof(actor)
        proof.revalidate(actor=actor)
        from app.composition.school_fact_education_gateway import EducationToolGateway
        from app.composition.school_fact_education_product import EducationProductAdapter
        from app.composition.school_fact_education_views import EducationMaintenanceViews
        from app.platform.assistant.runtime import AssistantRuntimeError
        from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

        # The model-wait boundary is deterministic: obtain actual gateway material,
        # commit a real revoke while no Reader connection is held, then release.
        selected = [actor]
        gateway = EducationToolGateway(
            resolve_actor=lambda _: SimpleNamespace(actor=selected[0], session_binding=marker),
            query=None,
            product=None,
            describe_adapter=EducationMaintenanceViews(
                EducationProductAdapter(repository=repository), directory=None, family=None
            ),
        )
        tool_context = ToolCallContext(marker, marker, marker, marker, 1, uuid4())
        call = ModelToolCall(
            call_id=marker,
            name="school_facts.education.records",
            arguments={"fact_kind": "education_class"},
        )
        turn = gateway.for_turn(marker)
        material = turn.execute(call, tool_context)
        assert material.model_content["matched_count"] == 1
        assert material.history_check.valid()
        assert held[0] == 0
        receipt = governance.revoke_grant(
            RevokeGrantCommand(
                school,
                grants["education_class"].target_id,
                1,
                now(),
                command_actor(),
                marker + ":revoke",
            )
        )
        assert receipt.assignment_epoch > actor.authorization_version
        assert not material.history_check.valid()
        with pytest.raises(AssistantRuntimeError):
            turn.revalidate(marker)
        with pytest.raises(EducationAuthorityDenied):
            proof.revalidate(actor=actor)
        assert held[0] == 0
        with pytest.raises(EducationAuthorityDenied):
            proof.revalidate(actor=actor)
        grant("education_class", "replacement")
        actor, identity, context, service = current()
        selected[0] = actor
        # Empty authorized exact-name reads carry the same release proof. A new
        # matching value in the allowed object invalidates the previous absence.
        empty_turn = gateway.for_turn(marker)
        empty = empty_turn.execute(
            ModelToolCall(
                call_id=marker + "empty",
                name=call.name,
                arguments={"fact_kind": "education_class", "query_text": marker},
            ),
            tool_context,
        )
        assert empty.model_content["matched_count"] == 0 and empty.history_check.valid()
        apply(
            writer,
            [
                dict(
                    fact_kind="education_class",
                    source_key=keys["education_class"],
                    operation="upsert",
                    expected_version=1,
                    effective_at=now(),
                    content={"display_name": marker},
                )
            ],
        )
        assert not empty.history_check.valid()
        with pytest.raises(AssistantRuntimeError):
            empty_turn.revalidate(marker)
        assert held[0] == 0
        proof = read_proof(actor)
        with owner() as s:
            person = s.execute(
                text("SELECT source_key,version FROM school_people WHERE school_id=:s AND id=:p"),
                {"s": school, "p": actor.person_id},
            ).one()
        original = ReadOnlyGovernanceDependencies._root
        changed = []

        def change_after_real_root_read(self, **kwargs):
            result = original(self, **kwargs)
            if kwargs["kind"] == "organization" and not changed:
                changed.append(True)
                apply(
                    writer,
                    [
                        dict(
                            fact_kind="person",
                            source_key=person.source_key,
                            operation="upsert",
                            expected_version=person.version,
                            effective_at=now(),
                            content={
                                "display_name": "Owned root changed during proof",
                                "person_kind": "staff",
                                "lifecycle_status": "active",
                            },
                        )
                    ],
                )
            return result

        monkeypatch.setattr(ReadOnlyGovernanceDependencies, "_root", change_after_real_root_read)
        with pytest.raises(EducationAuthorityDenied):
            proof.revalidate(actor=actor)
        assert changed == [True] and held[0] == 0
        monkeypatch.setattr(ReadOnlyGovernanceDependencies, "_root", original)
        fresh_actor, *_ = current()
        fresh = read_proof(fresh_actor)
        fresh.revalidate(actor=fresh_actor)
        assert held[0] == 0
    finally:
        event.remove(engines["reader"], "checkout", checkout)
        event.remove(engines["reader"], "checkin", checkin)
