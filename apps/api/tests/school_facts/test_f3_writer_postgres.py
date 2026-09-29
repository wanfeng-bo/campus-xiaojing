"""Opt-in F3 Writer safety acceptance against the owned schema-27 fixture database.

Run after test_f3_family_postgres.py in the same isolated test database. The only
owner writes below are explicitly labelled test identity/Governance fixtures;
all facts under test use the independent School Fact Writer. No role/ACL changes,
bootstrap, provider calls, or original-school connections occur in this file.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_family_authority import (
    FamilyFactAuthorityDenied,
    create_family_authority_components,
)
from app.composition.school_fact_family_execution import FamilyFinalAuthorizationGuard
from app.composition.school_fact_family_query import (
    SqlFamilyReadRepository,
    create_family_query_service,
)
from app.composition.school_fact_family_settings import FamilyConfiguration, FamilyScopeBinding
from app.composition.school_fact_persistence import (
    SchoolFactPersistenceError,
    SqlAlchemySchoolFactPersistence,
)
from app.composition.school_facts import HmacSchoolFactConfirmationCredentialIssuer
from app.db.base import Base
from app.platform.contracts import GuardianActorContext, StaffActorContext
from app.platform.governance import models as governance_models  # noqa: F401
from app.platform.governance.contracts import GovernanceAuthorityQuery
from app.platform.identity.context import CurrentUser
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import (
    ActorContextResolver,
    ContextNoLongerValid,
    compose_command_context,
)
from app.platform.identity.session import IdentitySessionStore
from app.platform.school_facts.application import SchoolFactChangeRejected, SchoolFactChangeService
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from app.platform.school_facts.f3_policy import F3_FACT_FIELDS, F3FactScope
from tests.school_facts.test_f3_family_postgres import (
    KEY,
    existing_fixture_writer,
    public_test_database_settings,
    safe_test_engine,
)
from tests.test_f1_product_identity import MemoryRedis

pytestmark = pytest.mark.skipif(
    not os.environ.get("F3_TEST_DATABASE_CONFIG"), reason="owned F3 PG config required"
)


def canonical_changes(
    *, idempotency_key, base_version, kind, key, content, operation="upsert", expected=0
):
    """Pure test sample construction, validated offline before any database run."""
    return CanonicalFactChangeSetV1.model_validate(
        {
            "source_sha256": sha256(idempotency_key.encode()).hexdigest(),
            "base_version": base_version,
            "changes": (
                {
                    "operation": operation,
                    "fact_kind": kind,
                    "source_key": key,
                    "expected_version": expected,
                    "effective_at": datetime.now(UTC),
                    "origin": {"sheet_code": "f3-pg-writer-safety", "row_number": 1},
                    "content": content,
                },
            ),
        }
    )


def foundation_changes(entries, *, base_version):
    """One atomic foundation batch; actual targets/versions are supplied by fixture reads."""
    at = datetime.now(UTC)
    return CanonicalFactChangeSetV1.model_validate(
        {
            "source_sha256": sha256(b"f3-writer-safety-foundation-v1").hexdigest(),
            "base_version": base_version,
            "changes": tuple(
                {
                    "operation": "upsert",
                    "fact_kind": kind,
                    "source_key": key,
                    "expected_version": version,
                    "effective_at": at,
                    "origin": {"sheet_code": "f3-complete-identity-fixture", "row_number": i},
                    "content": content,
                }
                for i, (kind, key, version, content) in enumerate(entries, 1)
            ),
        }
    )


def safety_foundation_entries():
    """Complete new formal Staff chain plus a real second child; no legacy non-root updates."""
    labels = {
        "requester": "F3安全验收申请职工",
        "verifier": "F3安全验收独立核验职工",
        "adult": "F3安全验收家校关系成人",
    }
    entries = [
        (
            "person",
            "person:f3-writer-safety-" + alias,
            0,
            {"display_name": label, "person_kind": "staff", "lifecycle_status": "active"},
        )
        for alias, label in labels.items()
    ]
    entries.append(
        (
            "person",
            "person:f3-writer-safety-wrong-child",
            0,
            {
                "display_name": "F3安全验收原错误关联学生",
                "person_kind": "other",
                "lifecycle_status": "active",
            },
        )
    )
    entries.append(
        (
            "organization",
            "organization:f3-writer-safety",
            0,
            {
                "kind": "department",
                "display_name": "F3隔离授权测试组织",
                "display_path": "F3隔离授权测试组织",
                "status": "active",
            },
        )
    )
    for alias in labels:
        entries.append(
            (
                "login_mapping",
                "login:f3-writer-safety-" + alias,
                0,
                {
                    "person_source_key": "person:f3-writer-safety-" + alias,
                    "provider_code": "local_test",
                    "provider_tenant_key": "development",
                    "external_subject": "f3-writer-safety-" + alias,
                    "status": "confirmed",
                },
            )
        )
    for alias in labels:
        entries.append(
            (
                "assignment",
                "assignment:f3-writer-safety-" + alias,
                0,
                {
                    "person_source_key": "person:f3-writer-safety-" + alias,
                    "organization_source_key": "organization:f3-writer-safety",
                    "position_title": "F3安全验收职工",
                    "semantic_role_code": "teacher",
                    "assignment_kind": "substantive",
                    "appointment_source": "manual",
                    "approval_reference": "owned-F3-writer-safety-fixture",
                    "status": "confirmed",
                },
            )
        )
    entries.append(
        (
            "student",
            "student:f3-writer-safety-wrong-child",
            0,
            {"person_source_key": "person:f3-writer-safety-wrong-child", "status": "active"},
        )
    )
    return entries


@dataclass
class LiveIdentity:
    resolver: ActorContextResolver
    session: Any

    def staff(self) -> StaffActorContext:
        refs = [o for o in self.resolver.list_options() if o.kind == "staff_assignment"]
        assert len(refs) == 1, "Fixture must have one actual current Staff assignment"
        actor = self.resolver.resolve(refs[0].opaque_ref)
        assert type(actor) is StaffActorContext
        return actor

    def guardians(self) -> list[tuple[str, GuardianActorContext]]:
        result = []
        for option in self.resolver.list_options():
            if option.kind == "guardian_relationship":
                actor = self.resolver.resolve(option.opaque_ref)
                assert type(actor) is GuardianActorContext
                result.append((option.opaque_ref, actor))
        return result

    def envelope(self, context):
        return ContextProofSigner(KEY).sign_execution_context(
            context,
            session_nonce_hash=self.session.session_nonce_hash,
            proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


@dataclass
class Staged:
    service: SchoolFactChangeService
    context: Any
    changes: CanonicalFactChangeSetV1
    identity: LiveIdentity
    credential: str
    authority: Any
    permit: Any
    results: list[dict]

    def confirm(self):
        result = self.service.confirm(
            credential=self.credential,
            change_set=self.changes,
            context=self.context,
            session_binding=self.identity.session.session_nonce_hash,
        )
        self.results.append(
            {
                "batch_id": str(result.batch_id),
                "version": result.target_version,
                "changed_count": result.changed_count,
                "replayed": result.replayed,
            }
        )
        return result


class WriterScenario:
    def __init__(self, engines, school):
        self.engines, self.school = engines, school
        self.sessions = {
            role: sessionmaker(engine, expire_on_commit=False) for role, engine in engines.items()
        }
        self.owner = self.sessions["migration"]
        self.marker = "f3-pg-writer:" + uuid4().hex
        self.bindings: dict[tuple[UUID, str], FamilyScopeBinding] = {}
        self.results: list[dict] = []

    def evidence(self, case, checks):
        output = Path(os.environ.get("F3_PG_EVIDENCE", "/tmp/f3-pg-evidence.json")).with_name(
            "writer-boundaries-result.json"
        )
        prior = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
        prior[case] = {
            "school_id": str(self.school),
            "checks": checks,
            "writer_receipts": self.results,
            "observed_at": datetime.now(UTC).isoformat(),
        }
        output.write_text(json.dumps(prior, ensure_ascii=False, indent=2), encoding="utf-8")

    def complete_seed_staff_context(self):
        """Publish the complete new 12-fact prerequisite and reuse its original receipt.

        Three users come only from the controlled Identity namespace owner API.
        Person, organization, mapping, assignment and the wrong-child sample are
        formal Canonical creations; this prerequisite does not prove the F3 guard.
        """
        from app.platform.identity.service import find_or_create_namespaced_test_user

        source_hash = sha256(b"f3-writer-safety-foundation-v1").hexdigest()
        entries = safety_foundation_entries()
        with self.owner() as session:
            for alias in ("requester", "verifier", "adult"):
                user = find_or_create_namespaced_test_user(
                    session, "f3-writer-safety-" + alias, "F3安全验收独立测试账号"
                )
                assert (
                    user.is_active
                    and user.provider_code == "local_test"
                    and user.provider_tenant_key == "development"
                )
            prior = session.execute(
                text(
                    "SELECT id,target_fact_version,succeeded_units FROM master_data_import_batches "
                    "WHERE school_id=:school AND source_sha256=:source "
                    "AND mode='canonical_atomic_changes' AND status='applied'"
                ),
                {"school": self.school, "source": source_hash},
            ).one_or_none()
            if prior is not None:
                assert prior.succeeded_units == len(entries)
                self.results.append(
                    {
                        "batch_id": str(prior.id),
                        "version": prior.target_fact_version,
                        "changed_count": len(entries),
                        "prerequisite_not_f3_guard_proof": True,
                        "reused_original_receipt": True,
                    }
                )
                return
        actor, identity, _, service = existing_fixture_writer(self.engines)
        context = compose_command_context(actor, uuid4(), "f3-writer-safety-foundation-v1")
        with self.engines["reader"].connect() as connection:
            base = connection.scalar(
                text("SELECT app.school_fact_current_version(:school)"), {"school": self.school}
            )
        changes = foundation_changes(entries, base_version=base)
        report = service.preflight(
            school_id=actor.school_id,
            change_set=changes,
            context=context,
            session_binding=identity.session_nonce_hash,
        )
        if not report.is_clean:
            print(
                "F3 complete foundation preflight",
                json.dumps(
                    [
                        {
                            "row": issue.row_number,
                            "column": issue.column_code,
                            "error": issue.error_code,
                            "fact_kind": entries[issue.row_number - 1][0],
                        }
                        for issue in report.issues
                    ],
                    ensure_ascii=False,
                ),
            )
            pytest.fail("Complete Canonical foundation rejected by actual preflight", pytrace=False)
        prepared = service.prepare_confirmation(
            change_set=changes, context=context, session_binding=identity.session_nonce_hash
        )
        original_error = SqlAlchemySchoolFactPersistence._raise_db_error

        def safe_error(error):
            original = getattr(error, "orig", None)
            state = getattr(original, "sqlstate", None)
            constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
            diagnostic = getattr(original, "diag", None)
            primary = getattr(diagnostic, "message_primary", None)
            denied = (
                re.fullmatch(
                    r"permission denied for (table|function|schema|sequence) ([a-z_][a-z_0-9]*)",
                    primary,
                )
                if isinstance(primary, str)
                else None
            )
            object_hint = (
                tuple(denied.groups())
                if denied
                else tuple(
                    (field, value)
                    for field in ("table_name", "schema_name")
                    if isinstance(value := getattr(diagnostic, field, None), str)
                    and re.fullmatch(r"[a-z_][a-z_0-9]*", value)
                )
            )
            print(
                "F3 complete foundation failure",
                object_hint,
                type(original).__name__,
                state if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state) else None,
                constraint
                if isinstance(constraint, str) and re.fullmatch(r"[a-z0-9_]{1,120}", constraint)
                else None,
            )
            original_error(error)

        with pytest.MonkeyPatch.context() as diagnostics:
            diagnostics.setattr(
                SqlAlchemySchoolFactPersistence, "_raise_db_error", staticmethod(safe_error)
            )
            result = service.confirm(
                credential=prepared.credential,
                change_set=changes,
                context=context,
                session_binding=identity.session_nonce_hash,
            )
        assert result.changed_count == len(entries)
        self.results.append(
            {
                "batch_id": str(result.batch_id),
                "version": result.target_version,
                "changed_count": len(entries),
                "prerequisite_not_f3_guard_proof": True,
            }
        )

    def identity(self, subject: str | None = None) -> LiveIdentity:
        subject = (
            "f3-writer-safety-"
            + {None: "requester", "f3-teacher-parent": "verifier", "f3-verifier": "adult"}[subject]
        )
        with self.owner() as session:
            row = session.execute(
                text(
                    "SELECT id,external_subject,display_name,security_version FROM users "
                    "WHERE provider_code='local_test' AND provider_tenant_key='development' "
                    "AND external_subject=:subject AND is_active"
                ),
                {"subject": subject},
            ).one()
        user = CurrentUser(*row)
        redis = MemoryRedis()
        store = IdentitySessionStore(redis=redis, ttl_seconds=900)
        token = store.create(user)
        identity = store.read(token)
        assert identity is not None
        return LiveIdentity(
            ActorContextResolver(
                session_factory=self.sessions["runtime"],
                redis=redis,
                proof_signer=ContextProofSigner(KEY),
                current_user=user,
                identity_session=identity,
            ),
            identity,
        )

    @staticmethod
    def _add(session, name, **values):
        session.execute(insert(Base.metadata.tables[name]).values(**values))

    def grant(self, actor, *, kind, action, objects=(), creations=()):
        """Owned-test Governance fixture, never evidence of a production grant."""
        scope = F3FactScope(
            self.school, kind, tuple(objects), tuple(creations), tuple(F3_FACT_FIELDS[kind])
        )
        duty_code = (
            "f3_guardian_relationship_recorder"
            if kind == "guardian_relationship" and action == "request"
            else "f3_guardian_relationship_verifier"
            if kind == "guardian_relationship" and action in {"verify", "end", "revoke"}
            else f"f3_{kind}_{'reader' if action == 'read' else 'maintainer'}"
        )
        at = datetime.now(UTC) - timedelta(seconds=1)
        sid, gid = uuid4(), uuid4()
        with self.owner.begin() as session:
            did = session.scalar(
                text(
                    "SELECT id FROM governance_duties WHERE school_id=:school AND duty_code=:code"
                ),
                {"school": self.school, "code": duty_code},
            )
            if did is None:
                did = uuid4()
                self._add(
                    session,
                    "governance_duties",
                    id=did,
                    school_id=self.school,
                    duty_code=duty_code,
                    current_version=1,
                )
                self._add(
                    session,
                    "governance_duty_versions",
                    school_id=self.school,
                    duty_id=did,
                    display_name="F3隔离Writer安全验收职责",
                    description="TEST FIXTURE ONLY; not original-school authority",
                    is_active=True,
                    effective_from=at,
                    version=1,
                )
            self._add(
                session,
                "governance_scopes",
                id=sid,
                school_id=self.school,
                scope_key=self.marker + ":" + sid.hex,
                current_version=1,
            )
            self._add(
                session,
                "governance_scope_versions",
                school_id=self.school,
                scope_id=sid,
                schema_owner="school_facts.f3",
                schema_name="exact_family_fact_set",
                schema_version=1,
                reference_document=scope.document,
                reference_sha256=scope.document_sha256,
                is_active=True,
                effective_from=at,
                version=1,
            )
            self._add(
                session,
                "governance_grants",
                id=gid,
                school_id=self.school,
                assignment_id=actor.assignment_id,
                duty_id=did,
                current_version=1,
            )
            self._add(
                session,
                "governance_grant_versions",
                school_id=self.school,
                grant_id=gid,
                duty_id=did,
                duty_version=1,
                scope_id=sid,
                scope_version=1,
                basis_authority="isolated_test_fixture",
                basis_reference=self.marker,
                valid_from=at,
                effective_from=at,
                planned_valid_to=at + timedelta(hours=1),
                transition_kind="create",
                version=1,
            )
            session.execute(
                text(
                    "INSERT INTO governance_assignment_epochs(school_id,assignment_id,epoch) "
                    "VALUES(:school,:assignment,1) ON CONFLICT(school_id,assignment_id) "
                    "DO UPDATE SET epoch=governance_assignment_epochs.epoch+1"
                ),
                {"school": self.school, "assignment": actor.assignment_id},
            )
        code = f"school_facts.f3.{kind}.{action}"
        self.bindings[(actor.assignment_id, code)] = FamilyScopeBinding(
            assignment_id=actor.assignment_id, operation_code=code, scope_id=sid
        )
        return gid

    def revoke_fixture_grant(self, actor, grant_id):
        table = Base.metadata.tables["governance_grant_versions"]
        now = datetime.now(UTC)
        with self.owner.begin() as session:
            old = dict(
                session.execute(
                    select(table)
                    .where(table.c.grant_id == grant_id)
                    .order_by(table.c.version.desc())
                    .limit(1)
                )
                .mappings()
                .one()
            )
            old.update(
                id=uuid4(),
                version=old["version"] + 1,
                transition_kind="revoke",
                effective_from=now,
                revoked_at=now,
            )
            old.pop("created_at")
            session.execute(insert(table).values(**old))
            session.execute(
                text("UPDATE governance_grants SET current_version=:version WHERE id=:id"),
                {"id": grant_id, "version": old["version"]},
            )
            self._bump_epoch(session, actor)

    def _bump_epoch(self, session, actor):
        session.execute(
            text(
                "UPDATE governance_assignment_epochs SET epoch=epoch+1 "
                "WHERE school_id=:school AND assignment_id=:assignment"
            ),
            {"school": self.school, "assignment": actor.assignment_id},
        )

    def advance_assignment_fixture(self, actor):
        """Another currently authorized Staff changes the staged actor's assignment."""
        with self.owner() as session:
            row = session.execute(
                text(
                    "SELECT a.source_key,a.version,p.source_key AS person_key,"
                    "o.source_key AS organization_key,a.position_title,a.semantic_role_code,"
                    "a.assignment_kind,a.appointment_source,a.approval_reference "
                    "FROM staff_assignments a JOIN school_people p ON p.id=a.person_id "
                    "JOIN organization_units o ON o.id=a.organization_unit_id WHERE a.id=:id"
                ),
                {"id": actor.assignment_id},
            ).one()
        modifier = self.identity("f3-teacher-parent")
        modifier_actor = modifier.staff()
        assert modifier_actor.assignment_id != actor.assignment_id
        self.grant(modifier_actor, kind="assignment", action="maintain", objects=(row.source_key,))
        self.stage(
            modifier,
            kind="assignment",
            key=row.source_key,
            expected=row.version,
            content={
                "person_source_key": row.person_key,
                "organization_source_key": row.organization_key,
                "position_title": row.position_title + "（版本变更核验）",
                "semantic_role_code": row.semantic_role_code,
                "assignment_kind": row.assignment_kind,
                "appointment_source": row.appointment_source,
                "approval_reference": row.approval_reference,
                "status": "confirmed",
            },
        ).confirm()

    def stage(self, identity, *, kind, key, content, operation="upsert", expected=0):
        actor = identity.staff()
        context = compose_command_context(
            actor, uuid4(), key + ":" + operation + ":" + str(expected)
        )
        configuration = FamilyConfiguration(
            approval_reference="owned-isolated-PG-test-fixtures-only",
            scope_bindings=tuple(self.bindings.values()),
        )
        authority = create_family_authority_components(
            session_factory=self.sessions["reader"], configuration=configuration
        )
        write = SqlAlchemySchoolFactPersistence(
            self.sessions["writer"],
            identity.envelope,
            confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(KEY),
            final_authorization_guard=FamilyFinalAuthorizationGuard(authority.change_set_guard),
            family_actor_validator=authority.actor_validator,
        )
        # Match the real executor kernel: its preflight persistence runs inside
        # the independent Writer role. Thin and parent projections use Reader.
        service = SchoolFactChangeService(
            reader=write, writer=write, family_actor_validator=authority.actor_validator
        )
        version = write.for_request(context).current_school_fact_version(actor.school_id)
        changes = canonical_changes(
            idempotency_key=context.idempotency_key,
            base_version=version,
            kind=kind,
            key=key,
            content=content,
            operation=operation,
            expected=expected,
        )
        try:
            permit = authority.require_change_set(actor, changes, checked_at=datetime.now(UTC))
        except FamilyFactAuthorityDenied:
            action = authority.change_set_guard._action(changes.changes[0])
            code = f"school_facts.f3.{kind}.{action}"
            duty = (
                "f3_guardian_relationship_recorder"
                if kind == "guardian_relationship" and action == "request"
                else "f3_guardian_relationship_verifier"
                if kind == "guardian_relationship" and action in {"verify", "end", "revoke"}
                else f"f3_{kind}_maintainer"
            )
            safe_reasons = []
            from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies

            dependencies = ReadOnlyGovernanceDependencies(self.sessions["reader"])
            with self.owner() as diagnostic_owner:
                organization = diagnostic_owner.scalar(
                    text("SELECT organization_unit_id FROM staff_assignments WHERE id=:id"),
                    {"id": actor.assignment_id},
                )
                anchor_count = diagnostic_owner.scalar(
                    text("SELECT count(*) FROM school_database_anchor")
                )
            safe_reasons.append(("anchor_count", anchor_count))
            now = datetime.now(UTC)
            probes = (
                (
                    "assignment",
                    lambda: dependencies.read_assignment(
                        school_id=actor.school_id, assignment_id=actor.assignment_id, as_of=now
                    ),
                ),
                (
                    "person",
                    lambda: dependencies.read_person_root(
                        school_id=actor.school_id, person_id=actor.person_id, as_of=now
                    ),
                ),
                (
                    "organization",
                    lambda: dependencies.read_organization_root(
                        school_id=actor.school_id, organization_id=organization, as_of=now
                    ),
                ),
            )
            for label, probe in probes:
                try:
                    snapshot = probe()
                    safe_reasons.append(
                        (
                            label,
                            snapshot is not None,
                            getattr(snapshot, "version", None),
                            getattr(snapshot, "is_effective", None),
                        )
                    )
                except Exception as probe_error:
                    state = getattr(getattr(probe_error, "orig", None), "sqlstate", None)
                    safe_reasons.append(
                        (
                            label,
                            type(probe_error).__name__,
                            state
                            if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state)
                            else None,
                        )
                    )
            try:
                basis = authority.connection_authority.issue(
                    GovernanceAuthorityQuery(
                        actor.school_id,
                        actor.assignment_id,
                        duty,
                        self.bindings[(actor.assignment_id, code)].scope_id,
                        datetime.now(UTC),
                    )
                )
                safe_reasons.append(
                    (
                        "issued_dependency_versions",
                        tuple(
                            (d.authority, d.version)
                            for d in basis.dependency_versions
                            if d.authority in {"school_facts.assignment", "school_facts.person"}
                        ),
                    )
                )
            except Exception as error:
                seen = set()
                while error is not None and id(error) not in seen:
                    seen.add(id(error))
                    reason = getattr(error, "reason", None)
                    sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
                    safe_reasons.append(
                        (
                            type(error).__name__,
                            reason
                            if isinstance(reason, str) and re.fullmatch(r"[a-z_]+", reason)
                            else None,
                            sqlstate
                            if isinstance(sqlstate, str) and re.fullmatch(r"[A-Z0-9]{5}", sqlstate)
                            else None,
                        )
                    )
                    error = error.__cause__ or error.__context__
            raise AssertionError(
                f"Real F3 authority rejected; typed diagnostics={safe_reasons}"
            ) from None
        report = service.preflight(
            school_id=actor.school_id,
            change_set=changes,
            context=context,
            session_binding=identity.session.session_nonce_hash,
        )
        assert report.is_clean, report.issues
        prepared = service.prepare_confirmation(
            change_set=changes, context=context, session_binding=identity.session.session_nonce_hash
        )
        return Staged(
            service,
            context,
            changes,
            identity,
            prepared.credential,
            authority,
            permit,
            self.results,
        )

    def counts(self):
        names = (
            "school_people",
            "school_person_names",
            "school_person_profile_versions",
            "guardian_relationships",
            "school_fact_sources",
            "school_fact_change_lineage",
        )
        with self.owner() as session:
            values = {
                name: session.scalar(
                    text(f"SELECT count(*) FROM {name} WHERE school_id=:school"),
                    {"school": self.school},
                )
                for name in names
            }
            values["applied"] = session.scalar(
                text(
                    "SELECT count(*) FROM master_data_import_batches WHERE school_id=:school AND "
                    "status='applied'"
                ),
                {"school": self.school},
            )
        return values

    def assert_no_publication(self, staged, before):
        assert self.counts() == before
        with self.owner() as session:
            assert (
                session.scalar(
                    text(
                        "SELECT count(*) FROM school_fact_change_lineage WHERE school_id=:school "
                        "AND fact_source_key=:key"
                    ),
                    {"school": self.school, "key": staged.changes.changes[0].source_key},
                )
                == 0
            )
            assert (
                session.scalar(
                    text(
                        "SELECT count(*) FROM master_data_import_batches WHERE school_id=:school "
                        "AND confirmation_token_sha256=:digest AND status='applied'"
                    ),
                    {
                        "school": self.school,
                        "digest": sha256(staged.credential.encode()).hexdigest(),
                    },
                )
                == 0
            )


@pytest.fixture
def scenario(request):
    if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") != "1" or request.config.getoption(
        "showlocals"
    ):
        pytest.fail("F3 PG tests require disabled plugin autoload and no showlocals", pytrace=False)
    path = Path(os.environ["F3_TEST_DATABASE_CONFIG"])
    database = public_test_database_settings(path)["database"]
    assert re.fullmatch(r"f3_[0-9a-f]{32}_test", database), "Owned isolated F3 test database only"
    engines = {
        role: safe_test_engine(path, role) for role in ("migration", "runtime", "reader", "writer")
    }
    try:
        for role, engine in engines.items():
            with engine.connect() as connection:
                actual, session_user = connection.execute(
                    text("SELECT current_database(),session_user")
                ).one()
                assert actual == database
                assert (
                    session_user
                    == {
                        "migration": "campus_ai_migration",
                        "runtime": "campus_ai_runtime",
                        "reader": "campus_ai_school_fact_reader",
                        "writer": "campus_ai_school_fact_writer",
                    }[role]
                )
        with engines["migration"].connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260913_27"
            )
            schools = connection.execute(text("SELECT id,source_key FROM schools")).all()
            assert len(schools) == 1 and schools[0].source_key == "school:f3-pg-fixture"
            assert (
                connection.scalar(
                    text(
                        "SELECT fingerprint_sha256 FROM app_private.context_proof_keys WHERE "
                        "is_current"
                    )
                )
                == sha256(KEY).hexdigest()
            )
        scenario = WriterScenario(engines, schools[0].id)
        scenario.complete_seed_staff_context()
        yield scenario
    finally:
        for engine in engines.values():
            engine.dispose()


def person_content():
    return {
        "display_name": "Writer安全验收合成人物",
        "person_kind": "other",
        "lifecycle_status": "active",
    }


def test_current_thin_grant_confirms_once_with_real_writer_and_lineage(scenario):
    identity = scenario.identity()
    actor = identity.staff()
    key = scenario.marker + ":success"
    scenario.grant(actor, kind="person", action="maintain", creations=(key,))
    staged = scenario.stage(identity, kind="person", key=key, content=person_content())
    before = scenario.counts()
    first, replay = staged.confirm(), staged.confirm()
    assert first.batch_id == replay.batch_id and first.target_version == replay.target_version
    assert first.changed_count == replay.changed_count == 1 and replay.replayed
    after = scenario.counts()
    assert after["school_people"] == before["school_people"] + 1
    assert after["school_fact_change_lineage"] == before["school_fact_change_lineage"] + 1
    assert after["applied"] == before["applied"] + 1
    with scenario.owner() as session:
        assert (
            session.scalar(
                text("SELECT count(*) FROM school_fact_change_lineage WHERE batch_id=:id"),
                {"id": first.batch_id},
            )
            == 1
        )
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM audit_logs WHERE "
                    "event_type='school_facts.change_set.applied' AND target_id=:id"
                ),
                {"id": str(first.batch_id)},
            )
            == 1
        )

    scenario.evidence("current_grant_idempotency", {"one_fact_lineage_audit": True})


@pytest.mark.parametrize("invalidation", ["grant_revoked", "assignment_version_changed"])
def test_real_preflight_then_current_authority_loss_publishes_nothing(scenario, invalidation):
    identity = scenario.identity()
    actor = identity.staff()
    key = scenario.marker + ":" + invalidation
    grant = scenario.grant(actor, kind="person", action="maintain", creations=(key,))
    staged = scenario.stage(identity, kind="person", key=key, content=person_content())
    if invalidation == "grant_revoked":
        scenario.revoke_fixture_grant(actor, grant)
    else:
        scenario.advance_assignment_fixture(actor)
        assert scenario.identity().staff().assignment_version > actor.assignment_version
    with pytest.raises(FamilyFactAuthorityDenied):
        staged.authority.final_guard(
            staged.permit, actor, staged.changes, checked_at=datetime.now(UTC)
        )
    before = scenario.counts()
    with pytest.raises((SchoolFactChangeRejected, SchoolFactPersistenceError)):
        staged.confirm()
    scenario.assert_no_publication(staged, before)
    scenario.evidence(invalidation, {"old_preflight_invalid": True, "zero_publication": True})


def test_wrong_relationship_ends_before_new_pending_and_independent_verified_authority(scenario):
    requester = scenario.identity()
    verifier = scenario.identity("f3-teacher-parent")
    adult = scenario.identity("f3-verifier")
    request_actor, verify_actor = requester.staff(), verifier.staff()
    assert request_actor.person_id != verify_actor.person_id
    assert request_actor.assignment_id != verify_actor.assignment_id
    assert adult.staff().person_id not in {request_actor.person_id, verify_actor.person_id}
    assert adult.guardians() == []
    wrong, corrected = scenario.marker + ":wrong", scenario.marker + ":corrected"
    premature = scenario.marker + ":premature"
    scenario.grant(
        request_actor,
        kind="guardian_relationship",
        action="request",
        creations=(wrong, corrected, premature),
    )

    def content(student, status, predecessor=None):
        value = {
            "guardian_person_source_key": "person:f3-writer-safety-adult",
            "student_source_key": "student:f3-writer-safety-wrong-child"
            if student == "b"
            else "student:f3-a",
            "relationship_kind": "verified_guardian",
            "access_purpose": "guardian_student_profile",
            "verification_source": "school_review",
            "verification_reference": scenario.marker,
            "status": status,
        }
        if predecessor:
            value["review_of_relationship_source_key"] = predecessor
        return value

    requested = scenario.stage(
        requester,
        kind="guardian_relationship",
        key=wrong,
        content=content("b", "pending_verification"),
    ).confirm()
    assert requested.changed_count == 1 and adult.guardians() == []
    scenario.grant(verify_actor, kind="guardian_relationship", action="verify", objects=(wrong,))
    scenario.grant(verify_actor, kind="guardian_relationship", action="end", objects=(wrong,))
    scenario.stage(
        verifier,
        kind="guardian_relationship",
        key=wrong,
        expected=1,
        content=content("b", "confirmed"),
    ).confirm()
    old_options = adult.guardians()
    assert len(old_options) == 1
    old_ref, old_actor = old_options[0]
    repository = SqlFamilyReadRepository(scenario.sessions["reader"])
    assert repository.authority(old_actor) is not None
    # A correction before termination must fail in the actual Writer transaction.
    before = scenario.counts()
    with pytest.raises(SchoolFactChangeRejected):
        scenario.stage(
            requester,
            kind="guardian_relationship",
            key=premature,
            content=content("a", "pending_verification", wrong),
        ).confirm()
    assert scenario.counts() == before
    scenario.stage(
        verifier, kind="guardian_relationship", key=wrong, expected=2, operation="end", content=None
    ).confirm()
    assert adult.guardians() == [] and repository.authority(old_actor) is None
    scenario.stage(
        requester,
        kind="guardian_relationship",
        key=corrected,
        content=content("a", "pending_verification", wrong),
    ).confirm()
    assert adult.guardians() == [] and repository.authority(old_actor) is None
    scenario.grant(
        verify_actor, kind="guardian_relationship", action="verify", objects=(corrected,)
    )
    scenario.stage(
        verifier,
        kind="guardian_relationship",
        key=corrected,
        expected=1,
        content=content("a", "confirmed", wrong),
    ).confirm()
    choices = adult.guardians()
    assert len(choices) == 1
    selected_ref, new_actor = choices[0]
    assert new_actor.guardian_relationship_id != old_actor.guardian_relationship_id
    query = create_family_query_service(
        session_factory=scenario.sessions["reader"],
        current_actor=lambda actor: adult.resolver.resolve(selected_ref) == actor,
    )
    assert query.query(actor=new_actor).status == "available"
    assert query.query(actor=old_actor).status == "denied"
    assert repository.authority(old_actor) is None
    with pytest.raises(ContextNoLongerValid):
        adult.resolver.resolve(old_ref)
    with scenario.owner() as session:
        rows = (
            session.execute(
                text(
                    "SELECT latest.fact_source_key AS source_key,g.status,"
                    "g.requested_by_assignment_id,g.verified_by_assignment_id "
                    "FROM (SELECT DISTINCT ON (fact_source_key) fact_source_key,target_id "
                    "FROM school_fact_change_lineage WHERE school_id=:school "
                    "AND fact_kind='guardian_relationship' "
                    "AND fact_source_key IN (:wrong,:corrected) "
                    "ORDER BY fact_source_key,target_version DESC) latest "
                    "JOIN guardian_relationships g ON g.id=latest.target_id "
                    "WHERE g.school_id=:school"
                ),
                {"school": scenario.school, "wrong": wrong, "corrected": corrected},
            )
            .mappings()
            .all()
        )
        assert {r["source_key"]: r["status"] for r in rows} == {
            wrong: "ended",
            corrected: "confirmed",
        }
        assert all(r["requested_by_assignment_id"] == request_actor.assignment_id for r in rows)
        assert all(r["verified_by_assignment_id"] == verify_actor.assignment_id for r in rows)

    scenario.evidence(
        "guardian_correction",
        {
            "premature_correction_rejected": True,
            "old_relationship_ended": True,
            "new_pending_no_authority": True,
            "independent_current_verifier": True,
            "only_new_relationship_has_authority": True,
        },
    )
