"""Finite deployment recovery through the retained canonical fact transaction.

The deployment approval authorizes the single missing school slice. The staff
identity resolved AFTER its uncommitted restoration is a commit gate and receipt
attribution, never the source of ordinary school maintenance authority.
"""

# Retained deployment composition deliberately reuses the closed internal write boundary.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import argparse
import json
import os
import secrets
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_initialization import _no_staff_envelope
from app.composition.school_fact_initialization_recovery_contracts import (
    SchoolVersionRecoveryManifest,
)
from app.composition.school_fact_live_initialization import (
    LiveFirstMaintainer,
    load_reviewed_contract,
)
from app.composition.school_fact_live_initialization_contracts import (
    LiveInitializationConfiguration,
    LiveInitializationManifest,
    LiveInitializationSession,
)
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.db.rls_context import set_local_execution_context
from app.platform.audit.models import AuditLog
from app.platform.contracts import SchoolId, StaffActorContext
from app.platform.identity.context import CompleteIdentitySession
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import ActorContextResolver, compose_command_context
from app.platform.identity.session import SessionRedisClient
from app.platform.master_data_imports.models import (
    MasterDataImportBatch,
    MasterDataImportUnit,
    SchoolFactInitialization,
)
from app.platform.school_facts.application import SchoolFactChangeService
from app.platform.school_facts.contracts import canonical_plan_sha256

CHANNEL = "deployment_missing_school_version_recovery"


class LiveSchoolVersionRecovery:
    def __init__(
        self,
        configuration: LiveInitializationConfiguration,
        original_manifest: LiveInitializationManifest,
        recovery_manifest: SchoolVersionRecoveryManifest,
        identity: LiveInitializationSession,
        *,
        redis: SessionRedisClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        # Revalidate frozen models too: model_copy/model_construct cannot bypass approval.
        self.manifest = SchoolVersionRecoveryManifest.model_validate_json(
            recovery_manifest.model_dump_json()
        )
        self.original = LiveInitializationManifest.model_validate_json(
            original_manifest.model_dump_json(exclude_none=True)
        )
        self.change_set = self.manifest.change_set(self.original)
        self.clock = clock
        self.live = LiveFirstMaintainer(
            configuration, self.original, identity, redis=redis, clock=clock
        )
        event.listen(self.live.sessions.class_, "before_commit", self._commit_window)
        self.key = "live-school-version-recovery:" + str(self.manifest.recovery_request_id)
        self.source_ref = "initialization-school-recovery:" + str(self.manifest.recovery_request_id)

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        self.live.__exit__(*args)

    @contextmanager
    def _transaction(self) -> Generator[Session]:
        # Original terminal-account checks do not renew its expired write window.
        self.live._check("close")
        with self.live.initializer._transaction(self.live.request) as session:
            yield session

    def _commit_window(self, session: Session) -> None:
        if session.info.get("school_version_recovery_write"):
            self.manifest.require_current(self.clock())

    def _original_record(self, session: Session) -> SchoolFactInitialization:
        record = session.scalar(
            select(SchoolFactInitialization)
            .where(SchoolFactInitialization.school_id == self.manifest.school_id)
            .with_for_update()
        )
        request = self.original.initialization_request()
        if record is None or (
            record.id != self.manifest.initialization_id
            or record.request_id != request.request_id
            or record.manifest_sha256 != request.manifest_sha256()
            or record.approval_reference != request.approval_reference
            or record.state not in {"applied", "closed"}
            or not isinstance(record.result_payload, dict)
            or record.result_payload.get("target_version") != 2
            or record.result_payload.get("changed_count") != 4
        ):
            raise ValueError("Original applied initialization binding differs.")
        return record

    def _batch(self, session: Session) -> MasterDataImportBatch | None:
        batch = session.scalar(
            select(MasterDataImportBatch)
            .where(
                MasterDataImportBatch.school_id == self.manifest.school_id,
                MasterDataImportBatch.source_ref == self.source_ref,
            )
            .with_for_update()
        )
        if batch is not None and (
            batch.source_sha256 != self.manifest.manifest_sha256
            or batch.request_id != self.manifest.recovery_request_id
            or batch.plan_sha256 != canonical_plan_sha256(self.change_set)
            or batch.mapping_payload != self._mapping()
        ):
            raise ValueError("Recovery batch binding differs.")
        return batch

    def _mapping(self) -> dict[str, Any]:
        return dict(
            channel=CHANNEL,
            authority="independently_reviewed_deployment_recovery",
            original_initialization_id=str(self.manifest.initialization_id),
            original_request_id=str(self.original.request_id),
            original_manifest_sha256=self.original.manifest_sha256,
            recovery_approval=self.manifest.model_dump(mode="json"),
        )

    def _require_missing(self, session: Session, record: SchoolFactInitialization) -> None:
        self.manifest.require_current(self.clock())
        if record.state != "applied":
            raise ValueError("Closed initialization forbids new recovery writes.")
        state = session.scalar(
            text(
                "SELECT state FROM public.governance_bootstrap_states "
                "WHERE school_id=:school FOR SHARE"
            ),
            {"school": self.manifest.school_id},
        )
        if state != "open":
            raise ValueError("Recovery requires the original open bootstrap.")
        root = session.execute(
            text(
                "SELECT source_key,school_code,display_name,timezone_name,status,version "
                "FROM public.schools WHERE id=:school"
            ),
            {"school": self.manifest.school_id},
        ).one()
        m = self.manifest
        if tuple(root) != (
            m.school_source_key,
            m.school_code,
            m.school_display_name,
            m.timezone_name,
            "active",
            1,
        ):
            raise ValueError("Reviewed original school root differs.")
        if session.scalar(
            text("SELECT count(*) FROM public.school_versions WHERE school_id=:school"),
            {"school": m.school_id},
        ):
            raise ValueError("Any existing school version forbids recovery.")
        if SqlAlchemySchoolFactPersistence._current_global_fact_version(session, m.school_id) != 2:
            raise ValueError("Original fact version changed.")

    def _principal(self, session: Session, record: SchoolFactInitialization):
        account = self.live._account()
        result = record.result_payload
        if not isinstance(result, dict) or not isinstance(result.get("targets"), list):
            raise ValueError("Original identity result is malformed.")
        targets = cast(list[dict[str, Any]], result["targets"])
        by_kind = {row["fact_kind"]: row for row in targets}
        if len(targets) != 4 or set(by_kind) != {
            "person",
            "organization",
            "assignment",
            "login_mapping",
        }:
            raise ValueError("Original identity result differs.")
        roots = {
            "person": ("school_people", "active"),
            "organization": ("organization_units", "active"),
            "assignment": ("staff_assignments", "confirmed"),
            "login_mapping": ("account_person_bindings", "confirmed"),
        }
        original_changes = {
            change.fact_kind: change for change in self.original.first_facts.changes
        }
        for kind, (table, status) in roots.items():
            target = by_kind[kind]
            root = session.execute(
                text(
                    f"SELECT source_key,version,status FROM public.{table} "
                    "WHERE school_id=:school AND id=:target FOR SHARE"
                ),
                {"school": self.manifest.school_id, "target": UUID(target["target_id"])},
            ).one_or_none()
            if root is None or tuple(root) != (
                original_changes[kind].source_key,
                target["target_version"],
                status,
            ):
                raise ValueError("Original first facts changed after initialization.")
        person = UUID(by_kind["person"]["target_id"])
        assignment = UUID(by_kind["assignment"]["target_id"])
        row = session.execute(
            text(
                "SELECT a.version FROM public.staff_assignments a "
                "JOIN public.account_person_bindings b ON b.school_id=a.school_id "
                "AND b.person_id=a.person_id WHERE a.school_id=:school "
                "AND a.id=:assignment AND a.person_id=:person AND b.id=:binding "
                "AND b.account_user_id=:user AND a.status='confirmed' "
                "AND b.status='confirmed' AND a.valid_from<=now() AND b.valid_from<=now() "
                "AND (a.valid_to IS NULL OR a.valid_to>now()) "
                "AND (b.valid_to IS NULL OR b.valid_to>now()) FOR SHARE OF a,b"
            ),
            dict(
                school=self.manifest.school_id,
                assignment=assignment,
                person=person,
                binding=UUID(by_kind["login_mapping"]["target_id"]),
                user=account.current_user.id,
            ),
        ).one_or_none()
        if row is None or row[0] != by_kind["assignment"]["target_version"]:
            raise ValueError("Current recipient differs from the original assignment.")
        return account, person, assignment, row[0]

    def _preflight(self, session: Session) -> None:
        facts = SqlAlchemySchoolFactPersistence(
            sessionmaker(bind=session.connection(), join_transaction_mode="create_savepoint"),
            _no_staff_envelope,
        )
        report = SchoolFactChangeService(reader=facts, writer=facts).preflight(
            school_id=SchoolId(self.manifest.school_id), change_set=self.change_set
        )
        if not report.is_clean:
            raise ValueError("School recovery preflight rejected.")

    def prepare(self) -> dict[str, Any]:
        with self._transaction() as session:
            record = self._original_record(session)
            batch = self._batch(session)
            if batch is not None:
                if batch.status != "applied":
                    self._require_missing(session, record)
                    self._principal(session, record)
                    if batch.status != "ready_for_confirmation":
                        raise ValueError("Recovery batch is not confirmable.")
                return dict(
                    batch_id=str(batch.id),
                    state="applied" if batch.status == "applied" else "prepared",
                    credential=None,
                    recovery_manifest_sha256=self.manifest.manifest_sha256,
                )
            self._require_missing(session, record)
            account, person, assignment, version = self._principal(session, record)
            self._preflight(session)
            session.info["school_version_recovery_write"] = True
            credential = secrets.token_urlsafe(48)
            batch = MasterDataImportBatch(
                id=uuid4(),
                school_id=self.manifest.school_id,
                mode="canonical_atomic_changes",
                source_kind="manual",
                source_ref=self.source_ref,
                source_sha256=self.manifest.manifest_sha256,
                worksheet_names=["school"],
                mapping_version=1,
                mapping_payload=self._mapping(),
                status="ready_for_confirmation",
                planned_units=1,
                succeeded_units=0,
                failed_units=0,
                pending_units=1,
                request_id=self.manifest.recovery_request_id,
                idempotency_key_hash=sha256(self.key.encode()).hexdigest(),
                plan_sha256=canonical_plan_sha256(self.change_set),
                base_fact_version=2,
                target_fact_version=3,
                confirmation_token_sha256=sha256(credential.encode()).hexdigest(),
                confirmation_card_sha256=canonical_plan_sha256(self.change_set),
                confirmation_expires_at=min(
                    self.manifest.expires_at, self.clock() + timedelta(minutes=10)
                ),
                confirmation_session_sha256=account.identity_session.session_nonce_hash,
                staged_by_user_id=account.current_user.id,
                staged_by_person_id=person,
                staged_by_assignment_id=assignment,
                staged_assignment_version=version,
                staged_authorization_version=account.current_user.security_version,
            )
            session.add(batch)
            session.flush()
            payload = self.change_set.changes[0].model_dump(mode="json", exclude_none=True)
            encoded = json.dumps(
                payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
            session.add(
                MasterDataImportUnit(
                    school_id=self.manifest.school_id,
                    batch_id=batch.id,
                    sequence=1,
                    row_number=1,
                    unit_kind="school_fact",
                    status="ready",
                    payload={k: v for k, v in payload.items() if k != "content"},
                    payload_sha256=sha256(encoded).hexdigest(),
                    soft_anomalies=[],
                    initial_soft_anomalies=[],
                )
            )
            session.flush()
            self.manifest.require_current(self.clock())
            return dict(
                batch_id=str(batch.id),
                state="prepared",
                credential=credential,
                recovery_manifest_sha256=self.manifest.manifest_sha256,
            )

    def _resolve_restored_actor(
        self, session: Session, account: CompleteIdentitySession, assignment: UUID
    ) -> StaffActorContext:
        resolver = ActorContextResolver(
            session_factory=sessionmaker(
                bind=session.connection(), join_transaction_mode="create_savepoint"
            ),
            redis=self.live.redis,
            proof_signer=ContextProofSigner(self.live.config.runtime.rls_context_signing_key),
            current_user=account.current_user,
            identity_session=account.identity_session,
            clock=self.clock,
        )
        # Existing SET-only migration membership; invoke unchanged normal proof validators.
        session.execute(text("SET LOCAL ROLE campus_ai_context_definer"))
        try:
            options = resolver.list_options()
            if len(options) != 1 or options[0].kind != "staff_assignment":
                raise ValueError("Restored staff context must be unique.")
            actor = resolver.resolve(options[0].opaque_ref)
        finally:
            session.execute(text("SET LOCAL ROLE campus_ai_db_owner"))
        if (
            type(actor) is not StaffActorContext
            or actor.assignment_id != assignment
            or actor.school_id != self.manifest.school_id
        ):
            raise ValueError("Restored context differs from the approved recipient.")
        return actor

    def confirm(self, *, credential: str) -> dict[str, Any]:
        with self._transaction() as session:
            record = self._original_record(session)
            batch = self._batch(session)
            if batch is None or not secrets.compare_digest(
                batch.confirmation_token_sha256 or "", sha256(credential.encode()).hexdigest()
            ):
                raise ValueError("Original recovery confirmation required.")
            if batch.status == "applied":
                return dict(batch.result_payload or {}, batch_id=str(batch.id), replayed=True)
            self._require_missing(session, record)
            account, person, assignment, version = self._principal(session, record)
            if (
                batch.status != "ready_for_confirmation"
                or batch.confirmation_expires_at is None
                or batch.confirmation_expires_at <= self.clock()
                or batch.confirmation_session_sha256 != account.identity_session.session_nonce_hash
                or (
                    batch.staged_by_user_id,
                    batch.staged_by_person_id,
                    batch.staged_by_assignment_id,
                    batch.staged_assignment_version,
                    batch.staged_authorization_version,
                )
                != (
                    account.current_user.id,
                    person,
                    assignment,
                    version,
                    account.current_user.security_version,
                )
            ):
                raise ValueError("Current recovery confirmation identity or window differs.")
            self._preflight(session)
            facts = SqlAlchemySchoolFactPersistence(self.live.sessions, _no_staff_envelope)
            session.info["school_version_recovery_write"] = True
            facts.apply_canonical_units_in_transaction(
                session, batch=batch, change_set=self.change_set, school_id=self.manifest.school_id
            )
            actor = self._resolve_restored_actor(session, account, assignment)
            context = compose_command_context(actor, self.manifest.recovery_request_id, self.key)
            signer = ContextProofSigner(self.live.config.runtime.rls_context_signing_key)
            set_local_execution_context(
                session,
                signer.sign_execution_context(
                    context,
                    session_nonce_hash=account.identity_session.session_nonce_hash,
                    proof_expires_at=min(
                        self.manifest.expires_at, self.clock() + timedelta(seconds=60)
                    ),
                ),
            )
            now = self.clock()
            self.manifest.require_current(now)
            batch.confirmation_consumed_at = now
            batch.confirmed_by_user_id = actor.account_user_id
            batch.confirmed_by_person_id = actor.person_id
            batch.confirmed_by_assignment_id = actor.assignment_id
            batch.confirmed_at = now
            batch.status = "applied"
            batch.succeeded_units = 1
            batch.pending_units = 0
            batch.result_payload = dict(target_version=3, changed_count=1)
            session.add(
                AuditLog(
                    actor_user_id=actor.account_user_id,
                    school_id=actor.school_id,
                    actor_person_id=actor.person_id,
                    actor_assignment_id=actor.assignment_id,
                    actor_assignment_version=actor.assignment_version,
                    actor_context_type="assignment",
                    idempotency_key_hash=sha256(self.key.encode()).hexdigest(),
                    event_type="school_facts.deployment_recovery.applied",
                    target_type="master_data_import_batch",
                    target_id=str(batch.id),
                    before_summary={},
                    after_summary=dict(
                        state_code="deployment_recovery.applied",
                        changed_count=1,
                        authority="independently_reviewed_deployment_recovery",
                        recovery_manifest_sha256=self.manifest.manifest_sha256,
                        original_initialization_id=str(self.manifest.initialization_id),
                    ),
                    request_id=str(self.manifest.recovery_request_id),
                    reason="deployment.missing_school_version_recovery:"
                    + self.manifest.manifest_sha256,
                )
            )
            session.flush()
            # Existing technical kernel command type; authority is the dedicated audit/source above.
            facts._append_terminal_outcome(
                session=session, batch=batch, actor=actor, context=context, sequences=[1]
            )
            session.flush()
            self.manifest.require_current(self.clock())
            return dict(batch_id=str(batch.id), target_version=3, changed_count=1, replayed=False)

    def result(self) -> dict[str, Any] | None:
        with self._transaction() as session:
            self._original_record(session)
            batch = self._batch(session)
            if batch is None:
                return None
            return dict(
                batch_id=str(batch.id),
                state="applied" if batch.status == "applied" else "prepared",
                result=batch.result_payload,
                recovery_manifest_sha256=self.manifest.manifest_sha256,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "confirm", "result"))
    for name in ("config", "original-manifest", "manifest", "session-file", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--approved-sha256", required=True)
    parser.add_argument("--confirmation", type=Path)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("New protected output required.")
        recovery = load_reviewed_contract(SchoolVersionRecoveryManifest, args.manifest)
        if not secrets.compare_digest(recovery.manifest_sha256, args.approved_sha256):
            raise ValueError("Independent recovery approval differs.")
        config = load_reviewed_contract(LiveInitializationConfiguration, args.config)
        original = load_reviewed_contract(LiveInitializationManifest, args.original_manifest)
        identity = load_reviewed_contract(LiveInitializationSession, args.session_file)
        with LiveSchoolVersionRecovery(config, original, recovery, identity) as service:
            if args.action == "confirm":
                if args.confirmation is None:
                    raise ValueError("Original confirmation required.")
                from app.composition.school_fact_live_initialization import _read

                prepared = _read(args.confirmation)
                if prepared.get("recovery_manifest_sha256") != recovery.manifest_sha256:
                    raise ValueError("Confirmation approval differs.")
                result = service.confirm(credential=prepared["credential"])
            else:
                result = getattr(service, args.action)()
        nofollow = getattr(os, "O_NOFOLLOW")  # noqa: B009 - mandatory Linux deployment flag
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, default=str)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        print("School version recovery action completed; protected receipt written.")
        return 0
    except Exception:  # noqa: BLE001 - no sensitive configuration/identity/error body
        print("School version recovery rejected or outcome unknown; inspect original result.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
