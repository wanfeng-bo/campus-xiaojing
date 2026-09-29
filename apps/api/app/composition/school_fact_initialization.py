from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import NoReturn, cast
from uuid import UUID, uuid4

from sqlalchemy import Table, insert, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_initialization_readiness import FirstMaintainerReadiness
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.platform.contracts import CommandExecutionContext, SchoolId
from app.platform.master_data_imports.models import (
    MasterDataImportBatch,
    MasterDataImportUnit,
    SchoolFactInitialization,
)
from app.platform.school_facts.application import SchoolFactChangeService
from app.platform.school_facts.contracts import canonical_plan_sha256
from app.platform.school_facts.initialization_contracts import (
    INITIALIZATION_AUTHORITY,
    SchoolFactInitializationRejected,
    SchoolFactInitializationRequest,
    require_initialization_authority,
    require_initialization_replay,
)


@dataclass(frozen=True, slots=True)
class InitializationPreparation:
    initialization_id: UUID
    credential: str | None
    state: str
    result: dict[str, object] | None


def _no_staff_envelope(_context: CommandExecutionContext) -> NoReturn:
    raise SchoolFactInitializationRejected("External initialization cannot issue a staff context.")


class SchoolFactInitializer:
    """One-off migration trust boundary, deliberately absent from normal HTTP/Executor."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = session_factory
        self._clock = clock
        self._facts = SqlAlchemySchoolFactPersistence(session_factory, _no_staff_envelope)

    @contextmanager
    def _transaction(
        self, request: SchoolFactInitializationRequest, *, lock_school: bool = True
    ) -> Generator[Session]:
        try:
            with self._sessions() as session:
                with session.begin():
                    actual_user = session.execute(text("SELECT session_user")).scalar_one()
                    if actual_user != "campus_ai_migration":
                        raise SchoolFactInitializationRejected(
                            "Initialization requires actual migration identity."
                        )
                    session.execute(text("SET LOCAL ROLE campus_ai_db_owner"))
                    heads = tuple(
                        session.execute(text("SELECT version_num FROM public.alembic_version"))
                        .scalars()
                        .all()
                    )
                    require_initialization_authority(session_user=actual_user, alembic_heads=heads)
                    session.execute(
                        text(
                            "SELECT app.assert_school_database_anchor(:anchor_id, :school_id, "
                            ":system_identifier)"
                        ),
                        {
                            "anchor_id": request.anchor_id,
                            "school_id": request.school_id,
                            "system_identifier": request.expected_system_identifier,
                        },
                    )
                    if lock_school:
                        session.execute(
                            text("SELECT id FROM public.schools WHERE id = :school_id FOR UPDATE"),
                            {"school_id": request.school_id},
                        ).scalar_one()
                    yield session
        except SQLAlchemyError:
            raise SchoolFactInitializationRejected(
                "Initialization failed or commit outcome is unknown; "
                "inspect the same manifest result."
            ) from None

    @staticmethod
    def _existing(
        session: Session, request: SchoolFactInitializationRequest
    ) -> SchoolFactInitialization | None:
        return session.scalar(
            select(SchoolFactInitialization)
            .where(SchoolFactInitialization.school_id == request.school_id)
            .with_for_update()
        )

    def _preflight(self, session: Session, request: SchoolFactInitializationRequest) -> None:
        self._require_school_version(session, request)
        # Reuse the real preflight on the same database transaction via savepoints.
        facts = SqlAlchemySchoolFactPersistence(
            sessionmaker(bind=session.connection(), join_transaction_mode="create_savepoint"),
            _no_staff_envelope,
        )
        report = SchoolFactChangeService(reader=facts, writer=facts).preflight(
            school_id=SchoolId(request.school_id), change_set=request.change_set
        )
        if not report.is_clean:
            raise SchoolFactInitializationRejected("Initialization preflight rejected.")
        for table in ("school_people", "staff_assignments", "account_person_bindings"):
            count = session.execute(
                text(f"SELECT count(*) FROM public.{table} WHERE school_id = :school_id"),
                {"school_id": request.school_id},
            ).scalar_one()
            if count:
                raise SchoolFactInitializationRejected("First maintainer facts already exist.")
        mapping = next(
            change.content
            for change in request.change_set.changes
            if change.fact_kind == "login_mapping"
        )
        if mapping is None:
            raise SchoolFactInitializationRejected("Initialization identity mapping is missing.")
        identity_id = session.execute(
            text("SELECT app.lock_existing_identity_user_id(:provider, :tenant, :subject)"),
            {
                "provider": mapping.provider_code,
                "tenant": mapping.provider_tenant_key,
                "subject": mapping.external_subject,
            },
        ).scalar_one_or_none()
        if identity_id is None:
            raise SchoolFactInitializationRejected(
                "Identity must establish the authentication account first."
            )

    def _require_school_version(
        self, session: Session, request: SchoolFactInitializationRequest
    ) -> None:
        if any(change.fact_kind == "school" for change in request.change_set.changes):
            return
        exists = session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM public.school_versions "
                "WHERE school_id=:school AND lifecycle_status='active' "
                "AND valid_from<=:as_of AND (valid_to IS NULL OR valid_to>:as_of))"
            ),
            {"school": request.school_id, "as_of": self._clock()},
        )
        if exists is not True:
            raise SchoolFactInitializationRejected(
                "First facts must include the school when its effective version is missing."
            )

    def prepare(self, request: SchoolFactInitializationRequest) -> InitializationPreparation:
        digest = request.manifest_sha256()
        token = secrets.token_urlsafe(48)
        with self._transaction(request) as session:
            record = self._existing(session, request)
            if record is not None:
                require_initialization_replay(
                    state=record.state,
                    original_sha256=record.manifest_sha256,
                    request_sha256=digest,
                    action="prepare",
                )
                if record.state == "applied":
                    return InitializationPreparation(
                        record.id, None, record.state, record.result_payload
                    )
            self._preflight(session, request)
            if record is None:
                record = SchoolFactInitialization(
                    id=uuid4(),
                    school_id=request.school_id,
                    request_id=request.request_id,
                    authority_reference=INITIALIZATION_AUTHORITY,
                    operator_reference=request.operator_reference,
                    approval_reference=request.approval_reference,
                    manifest_sha256=digest,
                    manifest_payload=json.loads(request.model_dump_json()),
                    state="prepared",
                    confirmation_token_sha256=sha256(token.encode()).hexdigest(),
                    expires_at=self._clock() + timedelta(minutes=10),
                )
                session.add(record)
            else:
                record.confirmation_token_sha256 = sha256(token.encode()).hexdigest()
                record.expires_at = self._clock() + timedelta(minutes=10)
            session.flush()
            result = InitializationPreparation(record.id, token, "prepared", None)
        return result

    def confirm(
        self, request: SchoolFactInitializationRequest, *, credential: str
    ) -> dict[str, object]:
        with self._transaction(request) as session:
            record = self._existing(session, request)
            if record is None:
                raise SchoolFactInitializationRejected("Initialization was not prepared.")
            require_initialization_replay(
                state=record.state,
                original_sha256=record.manifest_sha256,
                request_sha256=request.manifest_sha256(),
                action="confirm",
            )
            if not secrets.compare_digest(
                record.confirmation_token_sha256, sha256(credential.encode()).hexdigest()
            ):
                raise SchoolFactInitializationRejected("Initialization confirmation rejected.")
            if record.state == "applied":
                return dict(record.result_payload or {})
            now = self._clock()
            if record.expires_at <= now:
                raise SchoolFactInitializationRejected(
                    "Initialization confirmation expired; prepare again."
                )
            self._preflight(session, request)
            change_set = request.change_set
            batch_values: dict[str, object] = dict(
                id=uuid4(),
                school_id=request.school_id,
                initialization_id=record.id,
                mode="canonical_atomic_changes",
                source_kind="manual",
                source_ref=f"initialization:{record.id}",
                source_sha256=change_set.source_sha256,
                source_size_bytes=None,
                worksheet_names=sorted({c.origin.sheet_code for c in change_set.changes}),
                mapping_version=1,
                mapping_payload={"channel": "controlled_initialization", "schema_version": 1},
                status="ready_for_confirmation",
                planned_units=len(change_set.changes),
                succeeded_units=0,
                failed_units=0,
                pending_units=len(change_set.changes),
                request_id=request.request_id,
                idempotency_key_hash=record.manifest_sha256,
                plan_sha256=canonical_plan_sha256(change_set),
                base_fact_version=change_set.base_version,
                target_fact_version=change_set.base_version + 1,
                confirmation_token_sha256=record.confirmation_token_sha256,
                confirmation_card_sha256=canonical_plan_sha256(change_set),
                confirmation_expires_at=record.expires_at,
                confirmation_session_sha256=record.manifest_sha256,
            )
            session.execute(
                insert(cast(Table, MasterDataImportBatch.__table__)).values(**batch_values)
            )
            batch = session.get(MasterDataImportBatch, batch_values["id"])
            if batch is None:
                raise SchoolFactInitializationRejected("Initialization batch was not persisted.")
            for sequence, change in enumerate(change_set.changes, start=1):
                payload = change.model_dump(mode="json", exclude_none=True)
                encoded = json.dumps(
                    payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
                safe_payload = {key: value for key, value in payload.items() if key != "content"}
                session.add(
                    MasterDataImportUnit(
                        school_id=request.school_id,
                        batch_id=batch.id,
                        sequence=sequence,
                        row_number=change.origin.row_number,
                        unit_kind="school_fact",
                        status="ready",
                        payload=safe_payload,
                        payload_sha256=sha256(encoded).hexdigest(),
                        soft_anomalies=[],
                        initial_soft_anomalies=[],
                    )
                )
            session.flush()
            units = self._facts.apply_canonical_units_in_transaction(
                session, batch=batch, change_set=change_set, school_id=request.school_id
            )
            result: dict[str, object] = {
                "batch_id": str(batch.id),
                "target_version": change_set.base_version + 1,
                "changed_count": len(units),
                "targets": [
                    {
                        "fact_kind": change.fact_kind,
                        "source_key": change.source_key,
                        "target_id": str(unit.target_id),
                        "target_version": unit.target_version,
                    }
                    for unit, change in zip(units, change_set.changes, strict=True)
                ],
            }
            batch.confirmation_consumed_at = now
            batch.status = "applied"
            batch.succeeded_units = len(units)
            batch.pending_units = 0
            batch.result_payload = {
                "target_version": change_set.base_version + 1,
                "changed_count": len(units),
            }
            # Flush while the initialization is still prepared; terminal consistency is deferred.
            session.flush()
            record.state = "applied"
            record.applied_at = now
            record.result_payload = result
            session.flush()
        return result

    def result(self, request: SchoolFactInitializationRequest) -> dict[str, object] | None:
        with self._transaction(request) as session:
            record = self._existing(session, request)
            if record is None:
                return None
            if record.manifest_sha256 != request.manifest_sha256():
                raise SchoolFactInitializationRejected("Initialization manifest mismatch.")
            return {
                "initialization_id": str(record.id),
                "state": record.state,
                "result": record.result_payload,
            }

    @staticmethod
    def _lock_closure_epoch(session: Session, request: SchoolFactInitializationRequest) -> None:
        # Observe immutable applied result, then follow the publication epoch -> init lock order.
        record = session.scalar(
            select(SchoolFactInitialization).where(
                SchoolFactInitialization.school_id == request.school_id
            )
        )
        if record is None or record.result_payload is None or record.state == "closed":
            return
        assignment_id = FirstMaintainerReadiness.assignment_from_result(record.result_payload)
        session.execute(
            text(
                "SELECT epoch FROM public.governance_assignment_epochs "
                "WHERE school_id=:school_id AND assignment_id=:assignment_id FOR SHARE"
            ),
            {"school_id": request.school_id, "assignment_id": assignment_id},
        ).scalar_one()

    def close(
        self,
        request: SchoolFactInitializationRequest,
        *,
        readiness: FirstMaintainerReadiness,
    ) -> None:
        """Fixed real Identity + READ/MAINTAIN + closed Governance verification."""
        if type(readiness) is not FirstMaintainerReadiness:
            raise SchoolFactInitializationRejected(
                "A fixed initialization readiness verifier is required."
            )
        with self._transaction(request, lock_school=False) as session:
            self._lock_closure_epoch(session, request)
            record = self._existing(session, request)
            if record is None:
                raise SchoolFactInitializationRejected("Initialization was not prepared.")
            require_initialization_replay(
                state=record.state,
                original_sha256=record.manifest_sha256,
                request_sha256=request.manifest_sha256(),
                action="close",
            )
            if record.state == "closed":
                return
            if record.state != "applied" or record.result_payload is None:
                raise SchoolFactInitializationRejected("Initialization is not applied.")
            readiness.verify(session, request, record.result_payload, initialization_id=record.id)
            record.state = "closed"
            record.closed_at = self._clock()
            session.flush()
