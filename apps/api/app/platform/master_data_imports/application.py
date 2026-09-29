# pyright: strict

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from types import TracebackType
from typing import Literal, Protocol, Self, cast
from uuid import UUID, uuid5

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.db.rls_context import DatabaseExecutionContextEnvelope
from app.db.uow import CommitOutcomeUnknown, SqlAlchemyUnitOfWork
from app.platform.calendar.models import SchoolCalendarDay, SchoolCalendarVersion
from app.platform.calendar.ports import CalendarRepository
from app.platform.calendar.repository import SqlAlchemyWorkCalendarRepository
from app.platform.contracts import (
    CommandExecutionContext,
    SchoolId,
    StaffActorContext,
)
from app.platform.directory.models import SchoolPerson, SchoolPersonName
from app.platform.directory.ports import PersonRepository
from app.platform.directory.repository import SqlAlchemyPersonRepository
from app.platform.idempotency.contracts import (
    CommandIdempotencyIdentity,
    IdempotencyPayloadConflict,
    IdempotencyRecordProjection,
    SafeCommandResult,
)
from app.platform.idempotency.ports import (
    CommandIdempotencyProbeReader,
    CommandIdempotencyRepository,
)
from app.platform.idempotency.repository import (
    SqlAlchemyCommandIdempotencyProbeReader,
    SqlAlchemyCommandIdempotencyRepository,
)
from app.platform.identity import models as identity_models  # noqa: F401
from app.platform.master_data_imports.contracts import (
    AuthoritativeCalendarDraft,
    ConfirmImportBatchCommand,
    ImportApplyOutcome,
    ImportBatchPreview,
    ImportMode,
    ImportUnitDecision,
    ImportUnitPreview,
    PreflightImportBatchCommand,
    StageAuthoritativeCalendarImportCommand,
    StageManualPersonUnitCommand,
    StageOrdinaryPersonImportCommand,
    TakeOverOrdinaryImportCommand,
)
from app.platform.master_data_imports.models import (
    MasterDataImportBatch,
    MasterDataImportCommandOutcome,
    MasterDataImportTakeover,
    MasterDataImportUnit,
)
from app.platform.master_data_imports.ports import (
    LockedTakeoverAssignment,
    MasterDataImportAuditWriter,
    MasterDataImportOutcomeReader,
    MasterDataImportRecoveryReader,
    MasterDataImportRepository,
    SucceededImportAttemptOutcome,
)
from app.platform.master_data_imports.repository import (
    SqlAlchemyMasterDataImportRecoveryReader,
    SqlAlchemyMasterDataImportRepository,
)
from app.platform.messaging.contracts import OutboxEvent
from app.platform.messaging.ports import OutboxWriter
from app.platform.messaging.repository import SqlAlchemyOutboxWriter
from app.platform.organization.ports import StaffAssignmentMutationPort
from app.platform.organization.repository import SqlAlchemyOrganizationRepository
from app.platform.tenancy import models as tenancy_models  # noqa: F401

_MODEL_METADATA_DEPENDENCIES = (
    identity_models.User.__table__,
    tenancy_models.School.__table__,
)

_ID_NAMESPACE = UUID("15a6aa6d-15ff-519d-826f-75cdd257af20")
_SEMANTIC_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
_IDEMPOTENCY_RESOURCE_TYPE = "master_data.import_batch"
_IDEMPOTENCY_RESULT_TYPE = "master_data.import_outcome"


class MasterDataImportError(RuntimeError):
    """所有导入应用错误的稳定基类。"""


class MasterDataImportContextRequired(MasterDataImportError):
    """没有当前有效教职工任职。"""


class MasterDataImportPermissionDenied(MasterDataImportError):
    """服务端配置未授权当前语义职责。"""


class MasterDataImportVersionConflict(MasterDataImportError):
    """确认卡或数据库版本已经变化。"""


class MasterDataImportStateConflict(MasterDataImportError):
    """批次状态不允许当前动作。"""


class MasterDataImportHardConflict(MasterDataImportError):
    """外部编号、跨校引用或排他事实发生硬冲突。"""


class MasterDataImportIdempotencyConflict(MasterDataImportError):
    """同一操作者范围和幂等键被另一份规范请求复用。"""


class MasterDataImportOutcomeVerificationFailed(MasterDataImportError):
    """提交结果未知或幂等终态与不可变导入结果不一致。"""


@dataclass(frozen=True, slots=True)
class TakeoverCommitCheckpoint:
    """接管事务已提交的最小应用内检查点；不表示剩余单元已经执行。"""

    status: Literal["takeover_committed"]
    batch_id: UUID
    takeover_id: UUID
    batch_version_after: int
    new_idempotency_record_id: UUID
    transferred_sequences: tuple[int, ...]


class MasterDataImportAuthorizationFacts(Protocol):
    """服务端授权策略只消费已锁定的任职语义代码。"""

    def is_allowed(self, semantic_role_code: str) -> bool: ...


class ConfiguredMasterDataImportAuthorizer:
    """使用服务端配置的职责集合授权；空集合默认拒绝。"""

    def __init__(self, allowed_semantic_role_codes: frozenset[str]) -> None:
        if any(
            _SEMANTIC_CODE_PATTERN.fullmatch(code) is None for code in allowed_semantic_role_codes
        ):
            raise ValueError("主数据导入授权只能配置合法语义职责代码。")
        self._allowed = allowed_semantic_role_codes

    def is_allowed(self, semantic_role_code: str) -> bool:
        """仅按服务端配置判断被锁定的语义职责是否获准。"""

        return semantic_role_code in self._allowed


class BoundMasterDataImportAuditWriter:
    """将可信命令上下文绑定到数据库中的窄安全审计函数。"""

    def __init__(self, session: Session, context: CommandExecutionContext) -> None:
        self._session = session
        self._context = context

    def append_person_imported(
        self,
        *,
        batch_id: UUID,
        unit_id: UUID,
        person_id: UUID,
        source_kind: str,
        continued_same_name: bool,
    ) -> None:
        """以当前可信操作者上下文追加人员导入安全审计。"""

        reason = (
            "manual.person-import-confirmed"
            if source_kind == "manual"
            else "excel.person-import-confirmed"
        )
        self._append(
            batch_id=batch_id,
            unit_id=unit_id,
            event_type="master_data.person.imported",
            target_type="school_person",
            target_id=person_id,
            before_summary={
                "state_code": "person.absent",
                "resource_version": None,
                "stable_reference_ids": [],
                "fact_codes": [],
            },
            after_summary={
                "state_code": "person.confirmed",
                "resource_version": 1,
                "stable_reference_ids": [str(person_id)],
                "fact_codes": (
                    ["person.imported", "person.same_name_manually_continued"]
                    if continued_same_name
                    else ["person.imported"]
                ),
            },
            reason_code=reason,
        )

    def append_calendar_published(
        self,
        *,
        batch_id: UUID,
        unit_id: UUID,
        calendar_version_id: UUID,
    ) -> None:
        """以当前可信操作者上下文追加校历发布安全审计。"""

        self._append(
            batch_id=batch_id,
            unit_id=unit_id,
            event_type="master_data.calendar.published",
            target_type="school_calendar_version",
            target_id=calendar_version_id,
            before_summary={
                "state_code": "calendar.draft",
                "resource_version": 1,
                "stable_reference_ids": [str(calendar_version_id)],
                "fact_codes": [],
            },
            after_summary={
                "state_code": "calendar.published",
                "resource_version": 2,
                "stable_reference_ids": [str(calendar_version_id)],
                "fact_codes": ["calendar.published"],
            },
            reason_code="excel.calendar-published",
        )

    def append_ordinary_import_taken_over(
        self,
        *,
        batch_id: UUID,
        takeover_id: UUID,
    ) -> None:
        """以固定安全摘要追加普通导入人工接管审计。"""

        self._append(
            batch_id=batch_id,
            unit_id=None,
            event_type="master_data.import.taken_over",
            target_type="master_data_import_takeover",
            target_id=takeover_id,
            before_summary={
                "state_code": "ordinary_import.active_command",
                "resource_version": None,
                "stable_reference_ids": [str(batch_id)],
                "fact_codes": [],
            },
            after_summary={
                "state_code": "ordinary_import.taken_over",
                "resource_version": None,
                "stable_reference_ids": [str(batch_id), str(takeover_id)],
                "fact_codes": ["ordinary_import.taken_over"],
            },
            reason_code="manual.ordinary-import-taken-over",
        )

    def _append(
        self,
        *,
        batch_id: UUID,
        unit_id: UUID | None,
        event_type: str,
        target_type: str,
        target_id: UUID,
        before_summary: dict[str, object],
        after_summary: dict[str, object],
        reason_code: str,
    ) -> None:
        actor = self._context.actor
        if type(actor) is not StaffActorContext:
            raise TypeError("主数据导入审计只接受服务端签发的教职工上下文。")
        self._session.execute(
            text(
                """
                SELECT app.append_master_data_import_audit(
                    :actor_user_id, :school_id, :actor_person_id,
                    :actor_assignment_id, :actor_assignment_version,
                    :idempotency_key_hash, :event_type, :target_type,
                    :target_id, :batch_id, :unit_id,
                    :before_summary, :after_summary,
                    :request_id, :reason_code
                )
                """
            ).bindparams(
                bindparam("before_summary", type_=JSONB),
                bindparam("after_summary", type_=JSONB),
            ),
            {
                "actor_user_id": actor.account_user_id,
                "school_id": actor.school_id,
                "actor_person_id": actor.person_id,
                "actor_assignment_id": actor.assignment_id,
                "actor_assignment_version": actor.assignment_version,
                "idempotency_key_hash": sha256(
                    self._context.idempotency_key.encode("utf-8")
                ).hexdigest(),
                "event_type": event_type,
                "target_type": target_type,
                "target_id": target_id,
                "batch_id": batch_id,
                "unit_id": unit_id,
                "before_summary": before_summary,
                "after_summary": after_summary,
                "request_id": self._context.request_id,
                "reason_code": reason_code,
            },
        ).scalar_one()


class MasterDataImportUoW(Protocol):
    imports: MasterDataImportRepository
    idempotency: CommandIdempotencyRepository
    people: PersonRepository
    assignments: StaffAssignmentMutationPort
    calendars: CalendarRepository
    audit: MasterDataImportAuditWriter
    outbox: OutboxWriter

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def current_database_time(self) -> datetime: ...

    def commit(self) -> None: ...


class SqlAlchemyMasterDataImportUoW(SqlAlchemyUnitOfWork):
    """在一个 PostgreSQL 事务中组合导入所需窄端口。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        database_context_envelope: DatabaseExecutionContextEnvelope,
        context: CommandExecutionContext,
        lock_timeout_ms: int,
    ) -> None:
        super().__init__(session_factory, database_context_envelope)
        self._context = context
        self._lock_timeout_ms = lock_timeout_ms
        self.imports: MasterDataImportRepository
        self.idempotency: CommandIdempotencyRepository
        self.people: PersonRepository
        self.assignments: StaffAssignmentMutationPort
        self.calendars: CalendarRepository
        self.audit: MasterDataImportAuditWriter
        self.outbox: OutboxWriter

    def __enter__(self) -> Self:
        try:
            super().__enter__()
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == "28000":
                raise MasterDataImportContextRequired("当前教职工任职已经失效或变化。") from exc
            raise
        try:
            self.session.execute(
                select(func.set_config("lock_timeout", f"{self._lock_timeout_ms}ms", True))
            )
            self.imports = SqlAlchemyMasterDataImportRepository(self.session)
            self.idempotency = SqlAlchemyCommandIdempotencyRepository(self.session)
            self.people = SqlAlchemyPersonRepository(self.session)
            self.assignments = SqlAlchemyOrganizationRepository(self.session)
            self.calendars = SqlAlchemyWorkCalendarRepository(self.session)
            self.audit = BoundMasterDataImportAuditWriter(self.session, self._context)
            self.outbox = SqlAlchemyOutboxWriter(self.session)
        except BaseException as exc:
            super().__exit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    def current_database_time(self) -> datetime:
        """读取带时区的 PostgreSQL 当前时钟，避免信任应用主机时间。"""

        current = self.session.scalar(select(func.clock_timestamp()))
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise RuntimeError("PostgreSQL 没有返回带时区的数据库时钟。")
        return current


class MasterDataImportProbeUoW(Protocol):
    """只读响应丢失探测事务，不暴露 commit 或任何 Writer。"""

    idempotency: CommandIdempotencyProbeReader
    outcomes: MasterDataImportOutcomeReader
    recoveries: MasterDataImportRecoveryReader

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class SqlAlchemyMasterDataImportProbeUoW:
    """在新物理只读事务中组合幂等终态与不可变导入结果 Reader。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        database_context_envelope: DatabaseExecutionContextEnvelope,
    ) -> None:
        self._delegate = SqlAlchemyUnitOfWork(
            session_factory,
            database_context_envelope,
            read_only=True,
        )
        self.idempotency: CommandIdempotencyProbeReader
        self.outcomes: MasterDataImportOutcomeReader
        self.recoveries: MasterDataImportRecoveryReader

    def __enter__(self) -> Self:
        try:
            self._delegate.__enter__()
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == "28000":
                raise MasterDataImportContextRequired("当前教职工任职已经失效或变化。") from exc
            raise
        try:
            session = self._delegate.session
            self.idempotency = SqlAlchemyCommandIdempotencyProbeReader(session)
            recovery_reader = SqlAlchemyMasterDataImportRecoveryReader(session)
            self.outcomes = recovery_reader
            self.recoveries = recovery_reader
        except BaseException as exc:
            self._delegate.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._delegate.__exit__(exc_type, exc, traceback)


class MasterDataImportUoWFactory(Protocol):
    """为每条主数据导入命令创建隔离事务。"""

    def open(self, context: CommandExecutionContext) -> MasterDataImportUoW:
        """用可信命令上下文打开一个全新的导入事务。"""

        ...

    def open_probe(self, context: CommandExecutionContext) -> MasterDataImportProbeUoW:
        """用同一可信上下文打开全新的物理只读结果探测事务。"""

        ...


class SqlAlchemyMasterDataImportUoWFactory:
    """每个导入事务创建全新 UoW，避免跨命令复用 Session。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        envelope_factory: Callable[[CommandExecutionContext], DatabaseExecutionContextEnvelope],
        lock_timeout_ms: int,
    ) -> None:
        if lock_timeout_ms < 1:
            raise ValueError("导入锁等待预算必须为正整数毫秒。")
        self._session_factory = session_factory
        self._envelope_factory = envelope_factory
        self._lock_timeout_ms = lock_timeout_ms

    def open(self, context: CommandExecutionContext) -> SqlAlchemyMasterDataImportUoW:
        """绑定数据库上下文并返回尚未进入的全新 UoW。"""

        return SqlAlchemyMasterDataImportUoW(
            session_factory=self._session_factory,
            database_context_envelope=self._envelope_factory(context),
            context=context,
            lock_timeout_ms=self._lock_timeout_ms,
        )

    def open_probe(self, context: CommandExecutionContext) -> SqlAlchemyMasterDataImportProbeUoW:
        """创建新连接上的物理只读结果探测 UoW。"""

        return SqlAlchemyMasterDataImportProbeUoW(
            session_factory=self._session_factory,
            database_context_envelope=self._envelope_factory(context),
        )


class MasterDataImportService:
    """首批手工、普通原子和权威集合导入的唯一应用编排入口。"""

    def __init__(
        self,
        *,
        uow_factory: MasterDataImportUoWFactory,
        authorizer: MasterDataImportAuthorizationFacts,
    ) -> None:
        self._uow_factory = uow_factory
        self._authorizer = authorizer

    def stage_manual(
        self,
        command: StageManualPersonUnitCommand,
        *,
        context: CommandExecutionContext,
    ) -> ImportBatchPreview:
        """登记一个手工人员原子单元，返回人工确认所需预览。"""

        actor = self._staff_actor(context)
        batch_id = self._operation_id(context, action="stage-manual")
        unit_id = uuid5(batch_id, "unit:1")
        payload = command.unit.model_dump(mode="json")
        payload_sha256 = self._json_hash(payload)
        request_sha256 = self._json_hash(command.model_dump(mode="json"))
        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()
            self._authorize(uow, actor, now)
            existing = uow.imports.lock_batch(actor.school_id, batch_id)
            if existing is not None:
                existing_units = uow.imports.lock_units(actor.school_id, batch_id)
                if (
                    existing.mode != "manual_unit"
                    or existing.source_sha256 != request_sha256
                    or len(existing_units) != 1
                    or existing_units[0].payload_sha256 != payload_sha256
                ):
                    raise MasterDataImportIdempotencyConflict(
                        "同一幂等键已绑定另一份手工导入内容。"
                    )
                return self._stage_replay_preview(existing, existing_units)
            soft_anomaly_codes = (
                ("same_display_name",)
                if uow.imports.same_display_name_exists(
                    actor.school_id,
                    command.unit.display_name,
                )
                else ()
            )
            card_sha256 = self._card_hash(
                batch_id,
                ((1, 1, payload_sha256, soft_anomaly_codes),),
            )
            batch = MasterDataImportBatch(
                id=batch_id,
                school_id=actor.school_id,
                mode="manual_unit",
                source_kind="manual",
                source_ref=command.source_reference,
                source_sha256=request_sha256,
                filename_summary=None,
                source_size_bytes=None,
                worksheet_names=[],
                mapping_version=None,
                mapping_payload=None,
                status="ready_for_confirmation",
                planned_units=1,
                succeeded_units=0,
                failed_units=0,
                pending_units=1,
                confirmation_card_sha256=card_sha256,
                request_id=context.request_id,
                idempotency_key_hash=sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
                version=1,
            )
            unit = MasterDataImportUnit(
                id=unit_id,
                school_id=actor.school_id,
                batch_id=batch_id,
                sequence=1,
                row_number=command.unit.row_number,
                unit_kind="person",
                status="ready",
                payload=payload,
                payload_sha256=payload_sha256,
                soft_anomalies=[{"code": code} for code in soft_anomaly_codes],
                initial_soft_anomalies=[{"code": code} for code in soft_anomaly_codes],
                version=1,
            )
            uow.imports.add_batch(batch)
            uow.imports.add_unit(unit)
            preview = self._preview(batch, (unit,))
            uow.commit()
        return preview

    def stage_ordinary(
        self,
        command: StageOrdinaryPersonImportCommand,
        *,
        context: CommandExecutionContext,
    ) -> ImportBatchPreview:
        """登记经显式映射的普通 Excel 人员单元，不写入人员事实。"""

        actor = self._staff_actor(context)
        batch_id = self._operation_id(context, action="stage-ordinary")
        unit_values = tuple(
            (
                sequence,
                unit,
                unit.model_dump(mode="json"),
            )
            for sequence, unit in enumerate(command.units, start=1)
        )
        unit_hashes = tuple(
            (sequence, 1, self._json_hash(payload), ()) for sequence, _unit, payload in unit_values
        )
        card_sha256 = self._card_hash(batch_id, unit_hashes)
        mapping_payload: dict[str, object] = {
            "worksheet_name": command.worksheet_name,
            "mapping": command.mapping.model_dump(mode="json"),
        }
        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()
            self._authorize(uow, actor, now)
            existing = uow.imports.lock_batch(actor.school_id, batch_id)
            if existing is not None:
                existing_units = uow.imports.lock_units(actor.school_id, batch_id)
                expected_hashes = tuple(item[2] for item in unit_hashes)
                actual_hashes = tuple(unit.payload_sha256 for unit in existing_units)
                if (
                    existing.mode != "ordinary_atomic_units"
                    or existing.source_ref != command.source.opaque_ref
                    or existing.source_sha256 != command.source.source_sha256
                    or existing.filename_summary != command.source.filename_summary
                    or existing.source_size_bytes != command.source.size_bytes
                    or tuple(existing.worksheet_names) != command.source.worksheet_names
                    or existing.mapping_version != command.mapping.mapping_version
                    or existing.mapping_payload != mapping_payload
                    or actual_hashes != expected_hashes
                ):
                    raise MasterDataImportIdempotencyConflict(
                        "同一幂等键已绑定另一份普通导入内容。"
                    )
                return self._stage_replay_preview(existing, existing_units)
            source_batch = uow.imports.lock_batch_by_source(
                actor.school_id,
                source_kind="excel",
                source_ref=command.source.opaque_ref,
                source_sha256=command.source.source_sha256,
                mode="ordinary_atomic_units",
            )
            if source_batch is not None:
                raise MasterDataImportIdempotencyConflict(
                    "同一受保护 Excel 来源已经登记为普通导入批次。"
                )
            batch = MasterDataImportBatch(
                id=batch_id,
                school_id=actor.school_id,
                mode="ordinary_atomic_units",
                source_kind="excel",
                source_ref=command.source.opaque_ref,
                source_sha256=command.source.source_sha256,
                filename_summary=command.source.filename_summary,
                source_size_bytes=command.source.size_bytes,
                worksheet_names=list(command.source.worksheet_names),
                mapping_version=command.mapping.mapping_version,
                mapping_payload=mapping_payload,
                status="mapped",
                planned_units=len(unit_values),
                succeeded_units=0,
                failed_units=0,
                pending_units=len(unit_values),
                confirmation_card_sha256=card_sha256,
                request_id=context.request_id,
                idempotency_key_hash=sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
                version=1,
            )
            units: list[MasterDataImportUnit] = []
            uow.imports.add_batch(batch)
            for sequence, draft, payload in unit_values:
                unit = MasterDataImportUnit(
                    id=uuid5(batch_id, f"unit:{sequence}"),
                    school_id=actor.school_id,
                    batch_id=batch_id,
                    sequence=sequence,
                    row_number=draft.row_number,
                    unit_kind="person",
                    status="pending",
                    payload=payload,
                    payload_sha256=self._json_hash(payload),
                    soft_anomalies=[],
                    initial_soft_anomalies=[],
                    version=1,
                )
                uow.imports.add_unit(unit)
                units.append(unit)
            preview = self._preview(batch, tuple(units))
            uow.commit()
        return preview

    def stage_authoritative_calendar(
        self,
        command: StageAuthoritativeCalendarImportCommand,
        *,
        context: CommandExecutionContext,
    ) -> ImportBatchPreview:
        """登记权威校历集合草稿，不在本步骤发布当前校历。"""

        actor = self._staff_actor(context)
        batch_id = self._operation_id(context, action="stage-authoritative-calendar")
        calendar_version_id = uuid5(batch_id, "calendar-version")
        payload = command.draft.model_dump(mode="json")
        payload_sha256 = self._json_hash(payload)
        card_sha256 = self._card_hash(batch_id, ((1, 1, payload_sha256, ()),))
        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()
            self._authorize(uow, actor, now)
            existing = uow.imports.lock_batch(actor.school_id, batch_id)
            if existing is not None:
                existing_units = uow.imports.lock_units(actor.school_id, batch_id)
                if (
                    existing.mode != "authoritative_set_publish"
                    or existing.source_ref != command.source.opaque_ref
                    or existing.source_sha256 != command.source.source_sha256
                    or existing.filename_summary != command.source.filename_summary
                    or existing.source_size_bytes != command.source.size_bytes
                    or tuple(existing.worksheet_names) != command.source.worksheet_names
                    or len(existing_units) != 1
                    or existing_units[0].payload_sha256 != payload_sha256
                ):
                    raise MasterDataImportIdempotencyConflict(
                        "同一幂等键已绑定另一份权威集合内容。"
                    )
                return self._stage_replay_preview(existing, existing_units)
            source_batch = uow.imports.lock_batch_by_source(
                actor.school_id,
                source_kind="excel",
                source_ref=command.source.opaque_ref,
                source_sha256=command.source.source_sha256,
                mode="authoritative_set_publish",
            )
            if source_batch is not None:
                raise MasterDataImportIdempotencyConflict(
                    "同一受保护 Excel 来源已经登记为权威集合批次。"
                )
            batch = MasterDataImportBatch(
                id=batch_id,
                school_id=actor.school_id,
                mode="authoritative_set_publish",
                source_kind="excel",
                source_ref=command.source.opaque_ref,
                source_sha256=command.source.source_sha256,
                filename_summary=command.source.filename_summary,
                source_size_bytes=command.source.size_bytes,
                worksheet_names=list(command.source.worksheet_names),
                mapping_version=None,
                mapping_payload=None,
                status="ready_for_confirmation",
                planned_units=1,
                succeeded_units=0,
                failed_units=0,
                pending_units=1,
                confirmation_card_sha256=card_sha256,
                request_id=context.request_id,
                idempotency_key_hash=sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
                version=1,
            )
            version = SchoolCalendarVersion(
                id=calendar_version_id,
                school_id=actor.school_id,
                display_name=command.draft.display_name,
                timezone_name=command.draft.timezone_name,
                valid_from=command.draft.valid_from,
                valid_to=command.draft.valid_to,
                source_kind="master_data_import",
                source_ref=command.source.opaque_ref,
                status="draft",
                version=1,
            )
            unit = MasterDataImportUnit(
                id=uuid5(batch_id, "unit:1"),
                school_id=actor.school_id,
                batch_id=batch_id,
                sequence=1,
                row_number=1,
                unit_kind="calendar_set",
                status="ready",
                payload=payload,
                payload_sha256=payload_sha256,
                soft_anomalies=[],
                initial_soft_anomalies=[],
                target_id=calendar_version_id,
                target_version=1,
                version=1,
            )
            uow.imports.add_batch(batch)
            uow.calendars.add_version(version)
            for sequence, day in enumerate(command.draft.days, start=1):
                uow.calendars.add_day(
                    SchoolCalendarDay(
                        id=uuid5(calendar_version_id, f"day:{sequence}:{day.local_date}"),
                        school_id=actor.school_id,
                        calendar_version_id=calendar_version_id,
                        local_date=day.local_date,
                        day_kind=day.day_kind,
                        actual_start_time=day.actual_start_time,
                        actual_end_time=day.actual_end_time,
                        source_kind="master_data_import",
                        note=day.note,
                        version=1,
                    )
                )
            uow.imports.add_unit(unit)
            preview = self._preview(batch, (unit,))
            uow.commit()
        return preview

    def publish_calendar(
        self,
        command: ConfirmImportBatchCommand,
        *,
        context: CommandExecutionContext,
    ) -> ImportApplyOutcome:
        """在一个事务中校验确认卡并整体发布权威校历集合。"""

        actor = self._staff_actor(context)
        if command.decisions:
            raise MasterDataImportStateConflict("整体发布型校历不接受单元级跳过决定。")
        action_code = "master_data.import.calendar_publish"
        identity = self._idempotency_identity(
            command,
            context,
            actor,
            action_code=action_code,
        )
        try:
            return self._execute_calendar_publish(
                command,
                context=context,
                actor=actor,
                identity=identity,
                action_code=action_code,
            )
        except CommitOutcomeUnknown:
            return self._probe_outcome(context, identity, action_code=action_code)
        except IdempotencyPayloadConflict as exc:
            raise MasterDataImportIdempotencyConflict(
                "同一幂等键已绑定另一份校历发布请求。"
            ) from exc

    def _execute_calendar_publish(
        self,
        command: ConfirmImportBatchCommand,
        *,
        context: CommandExecutionContext,
        actor: StaffActorContext,
        identity: CommandIdempotencyIdentity,
        action_code: str,
    ) -> ImportApplyOutcome:
        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()
            self._authorize(uow, actor, now)
            record = uow.idempotency.acquire(identity)
            if record.status == "succeeded":
                return self._verified_replay_outcome(
                    uow.imports,
                    record,
                    identity,
                    action_code=action_code,
                )
            if record.status != "in_progress":
                raise MasterDataImportOutcomeVerificationFailed("该校历发布命令已有不可重放终态。")
            batch = uow.imports.lock_batch(actor.school_id, command.batch_id)
            if (
                batch is None
                or batch.mode != "authoritative_set_publish"
                or batch.status != "ready_for_confirmation"
            ):
                raise MasterDataImportStateConflict("该校历批次不允许发布。")
            if (
                batch.version != command.expected_batch_version
                or batch.confirmation_card_sha256 != command.confirmation_card_sha256
            ):
                raise MasterDataImportVersionConflict("校历确认卡已经变化，请重新预览。")
            units = uow.imports.lock_units(actor.school_id, batch.id)
            if (
                len(units) != 1
                or units[0].unit_kind != "calendar_set"
                or units[0].status != "ready"
                or units[0].target_id is None
            ):
                raise MasterDataImportStateConflict("校历草稿单元不完整。")
            unit = units[0]
            draft_version_id = unit.target_id
            if draft_version_id is None:
                raise MasterDataImportStateConflict("校历草稿缺少稳定版本引用。")
            versions = uow.calendars.lock_versions_for_publish(actor.school_id, draft_version_id)
            draft = next((item for item in versions if item.id == draft_version_id), None)
            if draft is None or draft.status != "draft" or draft.version != 1:
                raise MasterDataImportVersionConflict("目标校历草稿已经变化。")
            if self._json_hash(unit.payload) != unit.payload_sha256:
                raise MasterDataImportVersionConflict("校历草稿摘要已经变化。")
            expected_draft = AuthoritativeCalendarDraft.model_validate(
                unit.payload,
                strict=False,
            )
            if (
                draft.display_name != expected_draft.display_name
                or draft.timezone_name != expected_draft.timezone_name
                or draft.valid_from != expected_draft.valid_from
                or draft.valid_to != expected_draft.valid_to
                or draft.source_kind != "master_data_import"
                or draft.source_ref != batch.source_ref
            ):
                raise MasterDataImportVersionConflict("校历草稿版本事实已经变化。")
            stored_days = uow.calendars.lock_days_for_publish(actor.school_id, draft.id)
            expected_days = tuple(sorted(expected_draft.days, key=lambda day: day.local_date))
            if len(stored_days) != len(expected_days):
                raise MasterDataImportVersionConflict("校历草稿日记录数量已经变化。")
            for stored_day, expected_day in zip(stored_days, expected_days, strict=True):
                if (
                    stored_day.local_date != expected_day.local_date
                    or stored_day.day_kind != expected_day.day_kind
                    or stored_day.actual_start_time != expected_day.actual_start_time
                    or stored_day.actual_end_time != expected_day.actual_end_time
                    or stored_day.note != expected_day.note
                    or stored_day.source_kind != "master_data_import"
                    or stored_day.version != 1
                ):
                    raise MasterDataImportVersionConflict("校历草稿日期事实已经变化。")
            for previous in versions:
                if previous.id == draft.id or previous.status != "published":
                    continue
                overlaps = (previous.valid_to is None or previous.valid_to > draft.valid_from) and (
                    draft.valid_to is None or draft.valid_to > previous.valid_from
                )
                if not overlaps:
                    continue
                if previous.valid_from >= draft.valid_from:
                    raise MasterDataImportHardConflict(
                        "新校历不能覆盖起点相同或更晚的既有发布版本。"
                    )
                previous.valid_to = draft.valid_from
                previous.version += 1
                uow.calendars.save_version(previous)
            draft.status = "published"
            draft.version += 1
            uow.calendars.save_version(draft)
            unit.status = "published"
            unit.target_version = draft.version
            unit.version += 1
            batch.status = "published"
            batch.succeeded_units = 1
            batch.pending_units = 0
            batch.confirmed_by_user_id = actor.account_user_id
            batch.confirmed_by_person_id = actor.person_id
            batch.confirmed_by_assignment_id = actor.assignment_id
            batch.confirmed_at = now
            batch.version += 1
            uow.imports.save_unit(unit)
            uow.imports.save_batch(batch)
            outcome = ImportApplyOutcome(
                batch_id=batch.id,
                status="published",
                batch_version=batch.version,
                succeeded_units=(1,),
                failed_units=(),
                executed_units=(1,),
            )
            self._record_success_outcome(
                uow,
                record,
                identity,
                outcome,
                action_code=action_code,
            )
            uow.audit.append_calendar_published(
                batch_id=batch.id,
                unit_id=unit.id,
                calendar_version_id=draft.id,
            )
            uow.outbox.append(
                OutboxEvent(
                    event_id=uuid5(draft.id, "outbox:published"),
                    school_id=SchoolId(actor.school_id),
                    event_key=f"master-data.calendar-published:{draft.id}",
                    event_type="master_data.calendar.published",
                    aggregate_type="school_calendar_version",
                    aggregate_id=draft.id,
                    aggregate_version=draft.version,
                    occurred_at=now,
                )
            )
            uow.commit()
        return outcome

    def preflight(
        self,
        command: PreflightImportBatchCommand,
        *,
        context: CommandExecutionContext,
    ) -> ImportBatchPreview:
        """预检普通导入的硬冲突和软异常，不创建人员事实。"""

        actor = self._staff_actor(context)
        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()
            self._authorize(uow, actor, now)
            batch = uow.imports.lock_batch(actor.school_id, command.batch_id)
            if batch is None or batch.mode != "ordinary_atomic_units":
                raise MasterDataImportStateConflict("普通导入批次不存在于当前学校。")
            if batch.status != "mapped":
                raise MasterDataImportStateConflict("该批次不允许再次预检。")
            if (
                batch.version != command.expected_batch_version
                or batch.mapping_version != command.expected_mapping_version
            ):
                raise MasterDataImportVersionConflict("批次或字段映射版本已经变化。")
            units = uow.imports.lock_units(actor.school_id, batch.id)
            hard_failure = False
            for unit in units:
                payload = unit.payload
                external_reference = str(payload["external_reference"])
                if uow.imports.external_reference_exists(actor.school_id, external_reference):
                    unit.status = "preflight_failed"
                    unit.error_code = "external_reference_conflict"
                    unit.error_field_code = "校内编号"
                    hard_failure = True
                else:
                    unit.status = "ready"
                    if uow.imports.same_display_name_exists(
                        actor.school_id, str(payload["display_name"])
                    ):
                        unit.soft_anomalies = [{"code": "same_display_name"}]
                unit.version += 1
                uow.imports.save_unit(unit)
            batch.status = "preflight_failed" if hard_failure else "ready_for_confirmation"
            batch.version += 1
            batch.confirmation_card_sha256 = self._card_hash_for_models(batch.id, units)
            uow.imports.save_batch(batch)
            preview = self._preview(batch, units)
            uow.commit()
        return preview

    def preview(
        self,
        batch_id: UUID,
        *,
        context: CommandExecutionContext,
    ) -> ImportBatchPreview:
        """读取当前学校内已登记批次的服务端确认预览。"""

        actor = self._staff_actor(context)
        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()
            self._authorize(uow, actor, now)
            batch = uow.imports.lock_batch(actor.school_id, batch_id)
            if batch is None:
                raise MasterDataImportStateConflict("导入批次不存在于当前学校。")
            units = uow.imports.lock_units(actor.school_id, batch.id)
            return self._preview(batch, units)

    def confirm(
        self,
        command: ConfirmImportBatchCommand,
        *,
        context: CommandExecutionContext,
    ) -> ImportApplyOutcome:
        """按批次模式执行经人工确认的写入，不替人工判断事实真伪。"""

        actor = self._staff_actor(context)
        if self._batch_mode(command.batch_id, context=context) == "ordinary_atomic_units":
            return self._run_ordinary(
                command,
                context=context,
                expected_statuses=("ready_for_confirmation",),
                eligible_statuses=("ready",),
                action_code="master_data.import.ordinary_confirm",
            )
        action_code = "master_data.import.manual_confirm"
        identity = self._idempotency_identity(
            command,
            context,
            actor,
            action_code=action_code,
        )
        try:
            return self._execute_manual_confirm(
                command,
                context=context,
                actor=actor,
                identity=identity,
                action_code=action_code,
            )
        except CommitOutcomeUnknown:
            return self._probe_outcome(context, identity, action_code=action_code)
        except IdempotencyPayloadConflict as exc:
            raise MasterDataImportIdempotencyConflict(
                "同一幂等键已绑定另一份手工确认请求。"
            ) from exc

    def _execute_manual_confirm(
        self,
        command: ConfirmImportBatchCommand,
        *,
        context: CommandExecutionContext,
        actor: StaffActorContext,
        identity: CommandIdempotencyIdentity,
        action_code: str,
    ) -> ImportApplyOutcome:
        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()
            self._authorize(uow, actor, now)
            record = uow.idempotency.acquire(identity)
            if record.status == "succeeded":
                return self._verified_replay_outcome(
                    uow.imports,
                    record,
                    identity,
                    action_code=action_code,
                )
            if record.status != "in_progress":
                raise MasterDataImportOutcomeVerificationFailed("该手工确认命令已有不可重放终态。")
            batch = uow.imports.lock_batch(actor.school_id, command.batch_id)
            if batch is None:
                raise MasterDataImportStateConflict("导入批次不存在于当前学校。")
            units = uow.imports.lock_units(actor.school_id, batch.id)
            if batch.mode != "manual_unit" or batch.status != "ready_for_confirmation":
                raise MasterDataImportStateConflict("该批次不允许执行手工确认。")
            if (
                batch.version != command.expected_batch_version
                or batch.confirmation_card_sha256 != command.confirmation_card_sha256
            ):
                raise MasterDataImportVersionConflict("确认卡已经变化，请重新预览。")
            if len(units) != 1 or units[0].status != "ready":
                raise MasterDataImportStateConflict("手工导入单元状态不完整。")
            self._verify_locked_confirmation_card(batch, units, command)
            unit = units[0]
            payload = unit.payload
            external_reference = str(payload["external_reference"])
            if uow.imports.external_reference_exists(actor.school_id, external_reference):
                raise MasterDataImportHardConflict("校内外部编号已存在。")
            display_name = str(payload["display_name"])
            uow.imports.lock_display_name_candidate(actor.school_id, display_name)
            if uow.imports.same_display_name_exists(actor.school_id, display_name) and not any(
                anomaly.get("code") == "same_display_name" for anomaly in unit.soft_anomalies
            ):
                unit.soft_anomalies = [*unit.soft_anomalies, {"code": "same_display_name"}]
                unit.version += 1
                batch.version += 1
                batch.confirmation_card_sha256 = self._card_hash_for_models(batch.id, units)
                uow.imports.save_unit(unit)
                uow.imports.save_batch(batch)
                uow.commit()
                raise MasterDataImportVersionConflict("同名提示已经变化，请重新预览后确认。")
            if unit.soft_anomalies:
                if len(command.decisions) != 1:
                    raise MasterDataImportStateConflict("存在软异常时必须逐项填写人工继续理由。")
                decision = command.decisions[0]
                if (
                    decision.sequence != unit.sequence
                    or decision.expected_unit_version != unit.version
                ):
                    raise MasterDataImportVersionConflict("人工决定没有绑定当前手工单元版本。")
                if decision.continue_reason is None:
                    raise MasterDataImportStateConflict("存在软异常时必须填写人工继续理由。")
            elif command.decisions:
                raise MasterDataImportStateConflict("无软异常的手工单元不接受额外决定。")
            person_id = uuid5(batch.id, f"person:{unit.id}")
            person = SchoolPerson(
                id=person_id,
                school_id=actor.school_id,
                status="active",
                version=1,
            )
            valid_from = datetime.fromisoformat(str(payload["valid_from"]))
            name = SchoolPersonName(
                id=uuid5(person_id, "name:1"),
                school_id=actor.school_id,
                person_id=person_id,
                display_name=str(payload["display_name"]),
                valid_from=valid_from,
                valid_to=None,
                source_kind="person_external_reference",
                source_ref=external_reference,
                status="confirmed",
                version=1,
            )
            uow.people.add_person(person)
            uow.people.add_name(name)
            unit.status = "applied"
            unit.decision_reason = (
                command.decisions[0].continue_reason if command.decisions else None
            )
            unit.target_id = person_id
            unit.target_version = 1
            unit.version += 1
            batch.status = "applied"
            batch.succeeded_units = 1
            batch.pending_units = 0
            batch.confirmed_by_user_id = actor.account_user_id
            batch.confirmed_by_person_id = actor.person_id
            batch.confirmed_by_assignment_id = actor.assignment_id
            batch.confirmed_at = now
            batch.version += 1
            uow.imports.save_unit(unit)
            uow.imports.save_batch(batch)
            outcome = ImportApplyOutcome(
                batch_id=batch.id,
                status="applied",
                batch_version=batch.version,
                succeeded_units=(1,),
                failed_units=(),
                executed_units=(1,),
            )
            self._record_success_outcome(
                uow,
                record,
                identity,
                outcome,
                action_code=action_code,
            )
            uow.audit.append_person_imported(
                batch_id=batch.id,
                unit_id=unit.id,
                person_id=person_id,
                source_kind=batch.source_kind,
                continued_same_name=any(
                    anomaly.get("code") == "same_display_name" for anomaly in unit.soft_anomalies
                ),
            )
            uow.outbox.append(
                OutboxEvent(
                    event_id=uuid5(person_id, "outbox:imported"),
                    school_id=SchoolId(actor.school_id),
                    event_key=f"master-data.person-imported:{person_id}",
                    event_type="master_data.person.imported",
                    aggregate_type="school_person",
                    aggregate_id=person_id,
                    aggregate_version=1,
                    occurred_at=now,
                )
            )
            uow.commit()
        return outcome

    def resume(
        self,
        command: ConfirmImportBatchCommand,
        *,
        context: CommandExecutionContext,
    ) -> ImportApplyOutcome:
        """只重试普通导入中明确失败的单元，已经成功的单元不再执行。"""

        if self._batch_mode(command.batch_id, context=context) != "ordinary_atomic_units":
            raise MasterDataImportStateConflict("只有普通原子导入支持失败单元续跑。")
        return self._run_ordinary(
            command,
            context=context,
            expected_statuses=("partially_failed", "applying"),
            eligible_statuses=("failed", "pending", "ready"),
            action_code="master_data.import.ordinary_resume",
        )

    def take_over(
        self,
        command: TakeOverOrdinaryImportCommand,
        *,
        context: CommandExecutionContext,
    ) -> ImportApplyOutcome:
        """可靠提交接管检查点后，只继续数据库清单中的剩余普通单元。"""

        actor = self._staff_actor(context)
        with self._uow_factory.open(context) as authorization_uow:
            now = authorization_uow.current_database_time()
            self._authorize(authorization_uow, actor, now)
        identity = self._takeover_idempotency_identity(command, context, actor)
        try:
            recovered = self._probe_takeover_commit(
                command,
                context=context,
                identity=identity,
                allow_absent=True,
            )
            if isinstance(recovered, ImportApplyOutcome):
                return recovered
            checkpoint = recovered or self._execute_takeover_transaction(
                command,
                context=context,
                actor=actor,
                identity=identity,
            )
            return self._continue_taken_over_ordinary(
                command,
                checkpoint=checkpoint,
                context=context,
                actor=actor,
                identity=identity,
            )
        except CommitOutcomeUnknown as exc:
            recovered = self._probe_takeover_commit(
                command,
                context=context,
                identity=identity,
                allow_absent=False,
            )
            if isinstance(recovered, ImportApplyOutcome):
                return recovered
            if recovered is None:
                raise MasterDataImportOutcomeVerificationFailed(
                    "接管提交结果未知且没有找到可靠恢复事实。"
                ) from exc
            return self._continue_taken_over_ordinary(
                command,
                checkpoint=recovered,
                context=context,
                actor=actor,
                identity=identity,
            )
        except IdempotencyPayloadConflict as exc:
            raise MasterDataImportIdempotencyConflict(
                "接管幂等身份、旧命令终态或规范请求已经变化。"
            ) from exc

    def _execute_takeover_transaction(
        self,
        command: TakeOverOrdinaryImportCommand,
        *,
        context: CommandExecutionContext,
        actor: StaffActorContext,
        identity: CommandIdempotencyIdentity,
    ) -> TakeoverCommitCheckpoint:
        """按固定锁序完成旧命令终止、attempt 转移、接管事实与审计。"""

        action_code = "master_data.import.ordinary_takeover"
        decisions = {decision.sequence: decision for decision in command.units}
        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()

            # 1. 当前任职是第一把锁，且锁后立即重验授权。
            self._authorize(uow, actor, now)

            # 2. 只无锁预读 active id，用来定位下一把旧命令锁。
            old_record_id = uow.imports.find_active_idempotency_record_id(
                actor.school_id,
                command.batch_id,
            )
            if old_record_id is None:
                raise MasterDataImportStateConflict("普通导入批次没有可接管的活动命令。")

            # 3. 锁旧幂等记录并重验其活动命令归属；客户端不提供这些事实。
            old_record = uow.imports.lock_takeover_source(
                actor.school_id,
                command.batch_id,
                old_record_id,
            )
            if (
                old_record is None
                or old_record.status != "in_progress"
                or old_record.actor_context_type != "assignment"
                or old_record.actor_assignment_id is None
                or old_record.actor_assignment_version is None
                or old_record.action_code
                not in {
                    "master_data.import.ordinary_confirm",
                    "master_data.import.ordinary_resume",
                    "master_data.import.ordinary_takeover",
                }
                or old_record.resource_type != _IDEMPOTENCY_RESOURCE_TYPE
                or old_record.resource_id != str(command.batch_id)
            ):
                raise MasterDataImportStateConflict("旧活动命令已经变化或缺少可信任职归属。")

            # 4. 锁原任职。只有缺失或 person/version/status/有效期不匹配才允许接管。
            original_assignment = uow.imports.lock_takeover_assignment(
                actor.school_id,
                command.batch_id,
                old_record.record_id,
                old_record.actor_assignment_id,
            )
            original_assignment_invalid = self._original_takeover_assignment_is_invalid(
                original_assignment,
                expected_person_id=old_record.actor_person_id,
                expected_version=old_record.actor_assignment_version,
                now=now,
            )
            if not original_assignment_invalid:
                raise MasterDataImportStateConflict("原活动任职仍然有效，不能执行人工接管。")

            # 5. 新幂等记录在原任职之后 acquire 并锁定；后续拒绝会整体回滚该插入。
            acquisition = uow.idempotency.acquire_with_origin(identity)
            new_record = acquisition.record
            if not acquisition.created or new_record.status != "in_progress":
                raise MasterDataImportStateConflict("接管命令已经执行或缺少新的活动检查点。")

            # 6. 锁批次并重验预读、旧命令、学校、模式、状态与版本。
            batch = uow.imports.lock_batch(actor.school_id, command.batch_id)
            if (
                batch is None
                or batch.mode != "ordinary_atomic_units"
                or batch.status != "applying"
                or batch.active_idempotency_record_id != old_record.record_id
                or batch.active_action_code != old_record.action_code
            ):
                raise MasterDataImportStateConflict("批次活动命令已变化或不允许接管。")
            if batch.version != command.expected_batch_version:
                raise MasterDataImportVersionConflict("接管前批次版本已经变化。")

            # 7. 按 sequence 锁全体单元并立即重验确认卡、归属与版本。
            units = uow.imports.lock_units(actor.school_id, batch.id)
            self._verify_locked_takeover_confirmation_card(batch, units, command)
            incomplete_units = tuple(
                unit
                for unit in units
                if unit.last_attempt_idempotency_record_id == old_record.record_id
                and unit.last_attempt_completed is False
            )
            incomplete_sequences = {unit.sequence for unit in incomplete_units}
            if set(decisions) != incomplete_sequences:
                raise MasterDataImportStateConflict("接管决定必须恰好覆盖旧命令的全部未完成单元。")
            for unit in incomplete_units:
                decision = decisions[unit.sequence]
                if unit.version != decision.expected_unit_version:
                    raise MasterDataImportVersionConflict("接管单元版本已经变化。")
                if unit.soft_anomalies and decision.continue_reason is None:
                    raise MasterDataImportStateConflict("同名等软异常必须由接管人填写继续理由。")

            # 8. 最后锁事务级 previous-record advisory 唯一键并重验已有接管。
            existing_takeover = uow.imports.lock_takeover_by_previous_record(
                actor.school_id,
                old_record.record_id,
            )
            if existing_takeover is not None:
                raise MasterDataImportStateConflict("旧活动命令已经被其他人员接管。")

            batch_version_before = batch.version
            batch_version_after = batch_version_before + 1
            takeover_id = uuid5(new_record.record_id, "master-data-import-takeover")
            takeover = MasterDataImportTakeover(
                id=takeover_id,
                school_id=actor.school_id,
                batch_id=batch.id,
                previous_idempotency_record_id=old_record.record_id,
                new_idempotency_record_id=new_record.record_id,
                original_actor_user_id=old_record.actor_user_id,
                original_actor_person_id=old_record.actor_person_id,
                original_actor_assignment_id=old_record.actor_assignment_id,
                original_actor_assignment_version=old_record.actor_assignment_version,
                new_actor_user_id=actor.account_user_id,
                new_actor_person_id=actor.person_id,
                new_actor_assignment_id=actor.assignment_id,
                new_actor_assignment_version=actor.assignment_version,
                takeover_reason=command.takeover_reason,
                request_id=context.request_id,
                batch_version_before=batch_version_before,
                batch_version_after=batch_version_after,
            )
            uow.imports.add_takeover(takeover)
            transferred_sequences = tuple(takeover.transferred_sequences)
            if transferred_sequences != tuple(sorted(incomplete_sequences)):
                raise MasterDataImportOutcomeVerificationFailed(
                    "数据库生成的接管转移清单与已锁定的未完成单元不一致。"
                )
            for unit in incomplete_units:
                unit.last_attempt_idempotency_record_id = new_record.record_id
                unit.last_attempt_completed = False
                uow.imports.save_unit(unit)
            batch.active_idempotency_record_id = new_record.record_id
            batch.active_action_code = action_code
            batch.version = batch_version_after
            uow.imports.save_batch(batch)
            uow.imports.supersede_takeover_source(
                actor.school_id,
                batch.id,
                old_record.record_id,
                expected_request_sha256=old_record.request_sha256,
                batch_version_before=batch_version_before,
            )
            uow.commit()

        return TakeoverCommitCheckpoint(
            status="takeover_committed",
            batch_id=command.batch_id,
            takeover_id=takeover_id,
            batch_version_after=batch_version_after,
            new_idempotency_record_id=new_record.record_id,
            transferred_sequences=transferred_sequences,
        )

    def _continue_taken_over_ordinary(
        self,
        command: TakeOverOrdinaryImportCommand,
        *,
        checkpoint: TakeoverCommitCheckpoint,
        context: CommandExecutionContext,
        actor: StaffActorContext,
        identity: CommandIdempotencyIdentity,
    ) -> ImportApplyOutcome:
        """只消费已提交的数据库 manifest，并把 predecessor 已完成单元纳入首次结果。"""

        action_code = "master_data.import.ordinary_takeover"
        command_units = {unit.sequence: unit for unit in command.units}
        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()
            self._authorize(uow, actor, now)
            record = uow.idempotency.acquire(identity)
            if record.record_id != checkpoint.new_idempotency_record_id:
                raise MasterDataImportOutcomeVerificationFailed(
                    "接管检查点不属于当前固定幂等身份。"
                )
            if record.status == "succeeded":
                return self._verified_replay_outcome(
                    uow.imports,
                    record,
                    identity,
                    action_code=action_code,
                )
            if record.status != "in_progress":
                raise MasterDataImportOutcomeVerificationFailed("接管命令已有不可继续的终态。")
            takeover = uow.imports.find_takeover_by_new_record(
                actor.school_id,
                record.record_id,
            )
            batch = uow.imports.lock_batch(actor.school_id, command.batch_id)
            units = uow.imports.lock_units(actor.school_id, command.batch_id)
            if (
                takeover is None
                or takeover.id != checkpoint.takeover_id
                or takeover.batch_id != command.batch_id
                or takeover.batch_version_after != checkpoint.batch_version_after
                or tuple(takeover.transferred_sequences) != checkpoint.transferred_sequences
                or batch is None
                or batch.mode != "ordinary_atomic_units"
                or batch.status != "applying"
                or batch.active_idempotency_record_id != record.record_id
                or batch.active_action_code != action_code
            ):
                raise MasterDataImportOutcomeVerificationFailed(
                    "接管检查点、批次活动命令或版本证据不一致。"
                )
            manifest = dict(
                zip(
                    takeover.transferred_sequences,
                    takeover.transferred_unit_versions,
                    strict=True,
                )
            )
            if manifest != {
                sequence: decision.expected_unit_version
                for sequence, decision in command_units.items()
            }:
                raise MasterDataImportOutcomeVerificationFailed(
                    "客户端命令与数据库生成的接管清单不一致。"
                )
            units_by_sequence = {unit.sequence: unit for unit in units}
            incomplete_sequences = self._verified_takeover_manifest_progress(
                batch=batch,
                takeover=takeover,
                units_by_sequence=units_by_sequence,
                command_decisions={
                    sequence: (decision.expected_unit_version, decision.continue_reason)
                    for sequence, decision in command_units.items()
                },
                new_record_id=record.record_id,
            )
            accepted_attempt_ids = self._verified_takeover_attempt_lineage(
                uow.imports,
                school_id=actor.school_id,
                batch_id=command.batch_id,
                new_record_id=record.record_id,
            )
            selected_sequences = self._verified_ordinary_selected_sequences(
                recovery_reader=uow.imports,
                batch=batch,
                units=units,
                accepted_attempt_record_ids=accepted_attempt_ids,
            )
            if not selected_sequences or not set(manifest).issubset(selected_sequences):
                raise MasterDataImportOutcomeVerificationFailed(
                    "接管 attempt lineage 没有完整覆盖本次可执行单元。"
                )

        for sequence in incomplete_sequences:
            decision = command_units[sequence]
            self._apply_ordinary_person_unit(
                batch_id=command.batch_id,
                decision=ImportUnitDecision(
                    sequence=decision.sequence,
                    expected_unit_version=decision.expected_unit_version,
                    continue_reason=decision.continue_reason,
                ),
                context=context,
                eligible_statuses=("failed", "pending", "ready"),
                idempotency_record_id=checkpoint.new_idempotency_record_id,
            )

        return self._finalize_ordinary(
            command=command,
            context=context,
            actor=actor,
            identity=identity,
            action_code=action_code,
            selected_sequences=selected_sequences,
            accepted_attempt_record_ids=accepted_attempt_ids,
        )

    def _verified_takeover_manifest_progress(
        self,
        *,
        batch: MasterDataImportBatch,
        takeover: MasterDataImportTakeover,
        units_by_sequence: dict[int, MasterDataImportUnit],
        command_decisions: dict[int, tuple[int, str | None]],
        new_record_id: UUID,
    ) -> tuple[int, ...]:
        """精确证明 checkpoint 后每个 manifest 单元至多推进了一次。"""

        if len(units_by_sequence) != batch.planned_units or set(command_decisions) - set(
            units_by_sequence
        ):
            raise MasterDataImportOutcomeVerificationFailed(
                "数据库批次单元数量不完整或接管清单引用了不存在的单元。"
            )
        succeeded_units = sum(unit.status == "applied" for unit in units_by_sequence.values())
        failed_units = sum(unit.status == "failed" for unit in units_by_sequence.values())
        pending_units = sum(
            unit.status in {"pending", "ready"} for unit in units_by_sequence.values()
        )
        if (
            succeeded_units != batch.succeeded_units
            or failed_units != batch.failed_units
            or pending_units != batch.pending_units
            or succeeded_units + failed_units + pending_units != batch.planned_units
        ):
            raise MasterDataImportOutcomeVerificationFailed(
                "接管批次计数与全部单元当前事实不一致。"
            )

        incomplete: list[int] = []
        completed_count = 0
        for sequence, unit in units_by_sequence.items():
            decision = command_decisions.get(sequence)
            if decision is None:
                if unit.last_attempt_completed is not True or unit.status not in {
                    "applied",
                    "failed",
                }:
                    raise MasterDataImportOutcomeVerificationFailed(
                        "接管清单之外仍存在未完成单元。"
                    )
                continue
            expected_version, continue_reason = decision
            if (
                unit.last_attempt_idempotency_record_id != new_record_id
                or self._json_hash(unit.payload) != unit.payload_sha256
            ):
                raise MasterDataImportOutcomeVerificationFailed(
                    "接管 manifest 单元 owner 或载荷摘要不一致。"
                )
            if unit.last_attempt_completed is False:
                if unit.status not in {"pending", "ready", "failed"} or unit.version != (
                    expected_version
                ):
                    raise MasterDataImportOutcomeVerificationFailed(
                        "接管 manifest 未完成单元不是初始 eligible/version 形态。"
                    )
                incomplete.append(sequence)
                continue
            if unit.last_attempt_completed is not True or unit.version != expected_version + 1:
                raise MasterDataImportOutcomeVerificationFailed(
                    "接管 manifest 已完成单元没有精确推进一次 attempt。"
                )
            if unit.status == "applied":
                expected_target_id = uuid5(batch.id, f"person:{unit.id}")
                if (
                    unit.target_id != expected_target_id
                    or unit.target_version != 1
                    or unit.error_code is not None
                    or unit.error_field_code is not None
                    or unit.decision_reason != continue_reason
                ):
                    raise MasterDataImportOutcomeVerificationFailed(
                        "接管 manifest 已应用单元的 target/error 事实不一致。"
                    )
            elif unit.status == "failed":
                expected_failure_fields = {
                    "external_reference_conflict": "校内编号",
                    "soft_anomaly_changed": "教职工姓名",
                    "unit_transaction_failed": None,
                }
                if (
                    unit.target_id is not None
                    or unit.target_version is not None
                    or unit.error_code not in expected_failure_fields
                    or unit.error_field_code != expected_failure_fields[unit.error_code]
                    or unit.decision_reason is not None
                ):
                    raise MasterDataImportOutcomeVerificationFailed(
                        "接管 manifest 已失败单元的 target/error 事实不一致。"
                    )
            else:
                raise MasterDataImportOutcomeVerificationFailed(
                    "接管 manifest 已完成单元不是固定终态。"
                )
            completed_count += 1
        if batch.version != takeover.batch_version_after + completed_count:
            raise MasterDataImportOutcomeVerificationFailed(
                "接管批次版本没有与已完成 manifest attempt 精确对齐。"
            )
        return tuple(sorted(incomplete))

    def _verified_ordinary_selected_sequences(
        self,
        *,
        recovery_reader: MasterDataImportRecoveryReader,
        batch: MasterDataImportBatch,
        units: tuple[MasterDataImportUnit, ...],
        accepted_attempt_record_ids: frozenset[UUID],
    ) -> frozenset[int]:
        """只纳入当前 lineage 单元，并精确证明每个被排除的更早命令终态。"""

        if not accepted_attempt_record_ids:
            raise MasterDataImportOutcomeVerificationFailed("普通导入 attempt lineage 为空。")
        selected: set[int] = set()
        for unit in units:
            owner_record_id = unit.last_attempt_idempotency_record_id
            if owner_record_id in accepted_attempt_record_ids:
                if unit.last_attempt_completed is True:
                    self._verify_completed_unit_terminal_shape(batch=batch, unit=unit)
                selected.add(unit.sequence)
                continue
            self._verify_excluded_historical_unit(
                recovery_reader=recovery_reader,
                batch=batch,
                unit=unit,
            )
        return frozenset(selected)

    def _verify_excluded_historical_unit(
        self,
        *,
        recovery_reader: MasterDataImportRecoveryReader,
        batch: MasterDataImportBatch,
        unit: MasterDataImportUnit,
    ) -> None:
        """证明非 lineage owner 自身具有同批次、不可变且覆盖本单元的首次结果。"""

        owner_record_id = unit.last_attempt_idempotency_record_id
        if owner_record_id is None or unit.last_attempt_completed is not True:
            raise MasterDataImportOutcomeVerificationFailed(
                "被排除的普通导入单元没有已完成的 owner attempt。"
            )
        outcome = recovery_reader.find_succeeded_attempt_outcome(
            SchoolId(batch.school_id),
            batch.id,
            owner_record_id,
        )
        if (
            outcome is None
            or outcome.idempotency_record_id != owner_record_id
            or outcome.batch_id != batch.id
            or outcome.action_code
            not in {
                "master_data.import.ordinary_confirm",
                "master_data.import.ordinary_resume",
                "master_data.import.ordinary_takeover",
            }
        ):
            raise MasterDataImportOutcomeVerificationFailed(
                "被排除的普通导入单元缺少同批次精确 succeeded 命令首次结果。"
            )
        succeeded, failed, executed = self._validated_outcome_sequences(outcome)
        snapshots_by_sequence = {
            snapshot.sequence: snapshot for snapshot in outcome.unit_outcome_snapshots
        }
        if (
            len(snapshots_by_sequence) != len(outcome.unit_outcome_snapshots)
            or set(snapshots_by_sequence) != set(executed)
            or unit.sequence not in executed
            or (unit.status == "applied" and unit.sequence not in succeeded)
            or (unit.status == "failed" and unit.sequence not in failed)
        ):
            raise MasterDataImportOutcomeVerificationFailed(
                "被排除的普通导入单元与 owner 首次结果集合不一致。"
            )
        snapshot = snapshots_by_sequence[unit.sequence]
        if (
            snapshot.status != unit.status
            or snapshot.version != unit.version
            or snapshot.target_id != unit.target_id
            or snapshot.target_version != unit.target_version
            or snapshot.error_code != unit.error_code
            or snapshot.error_field_code != unit.error_field_code
        ):
            raise MasterDataImportOutcomeVerificationFailed(
                "被排除的普通导入单元与 owner 首次逐单元结果不一致。"
            )
        self._verify_completed_unit_terminal_shape(batch=batch, unit=unit)

    def _verify_completed_unit_terminal_shape(
        self,
        *,
        batch: MasterDataImportBatch,
        unit: MasterDataImportUnit,
    ) -> None:
        """校验已完成普通人员单元的 version、target、error 与失败理由固定形态。"""

        if (
            unit.last_attempt_completed is not True
            or unit.version < 2
            or self._json_hash(unit.payload) != unit.payload_sha256
        ):
            raise MasterDataImportOutcomeVerificationFailed(
                "已完成普通导入单元的 attempt、版本或载荷事实不一致。"
            )
        if unit.status == "applied":
            if (
                unit.target_id != uuid5(batch.id, f"person:{unit.id}")
                or unit.target_version != 1
                or unit.error_code is not None
                or unit.error_field_code is not None
            ):
                raise MasterDataImportOutcomeVerificationFailed(
                    "已应用普通导入单元的 target/error 事实不一致。"
                )
            return
        expected_failure_fields = {
            "unit_version_conflict": None,
            "external_reference_conflict": "校内编号",
            "soft_anomaly_changed": "教职工姓名",
            "unit_transaction_failed": None,
        }
        if (
            unit.status != "failed"
            or unit.target_id is not None
            or unit.target_version is not None
            or unit.error_code not in expected_failure_fields
            or unit.error_field_code != expected_failure_fields[unit.error_code]
            or unit.decision_reason is not None
        ):
            raise MasterDataImportOutcomeVerificationFailed(
                "已失败普通导入单元的 target/error/reason 事实不一致。"
            )

    @staticmethod
    def _original_takeover_assignment_is_invalid(
        original_assignment: LockedTakeoverAssignment | None,
        *,
        expected_person_id: UUID,
        expected_version: int,
        now: datetime,
    ) -> bool:
        """仅缺失或权威 person/version/status/有效期不匹配才视为原任职失效。"""

        return (
            original_assignment is None
            or original_assignment.person_id != expected_person_id
            or original_assignment.version != expected_version
            or original_assignment.status != "confirmed"
            or original_assignment.valid_from > now
            or (original_assignment.valid_to is not None and original_assignment.valid_to <= now)
        )

    def _batch_mode(
        self,
        batch_id: UUID,
        *,
        context: CommandExecutionContext,
    ) -> str:
        actor = self._staff_actor(context)
        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()
            self._authorize(uow, actor, now)
            batch = uow.imports.lock_batch(actor.school_id, batch_id)
            if batch is None:
                raise MasterDataImportStateConflict("导入批次不存在于当前学校。")
            return batch.mode

    def _run_ordinary(
        self,
        command: ConfirmImportBatchCommand,
        *,
        context: CommandExecutionContext,
        expected_statuses: tuple[str, ...],
        eligible_statuses: tuple[str, ...],
        action_code: str,
    ) -> ImportApplyOutcome:
        actor = self._staff_actor(context)
        identity = self._idempotency_identity(
            command,
            context,
            actor,
            action_code=action_code,
        )
        try:
            return self._execute_ordinary(
                command,
                context=context,
                actor=actor,
                identity=identity,
                expected_statuses=expected_statuses,
                eligible_statuses=eligible_statuses,
                action_code=action_code,
            )
        except CommitOutcomeUnknown:
            return self._probe_outcome(context, identity, action_code=action_code)
        except IdempotencyPayloadConflict as exc:
            raise MasterDataImportIdempotencyConflict(
                "同一幂等键已绑定另一份普通导入确认请求。"
            ) from exc

    def _execute_ordinary(
        self,
        command: ConfirmImportBatchCommand,
        *,
        context: CommandExecutionContext,
        actor: StaffActorContext,
        identity: CommandIdempotencyIdentity,
        expected_statuses: tuple[str, ...],
        eligible_statuses: tuple[str, ...],
        action_code: str,
    ) -> ImportApplyOutcome:
        decisions = {decision.sequence: decision for decision in command.decisions}
        to_execute: list[int] = []
        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()
            self._authorize(uow, actor, now)
            acquisition = uow.idempotency.acquire_with_origin(identity)
            record = acquisition.record
            if record.status == "succeeded":
                return self._verified_replay_outcome(
                    uow.imports,
                    record,
                    identity,
                    action_code=action_code,
                )
            if record.status != "in_progress":
                raise MasterDataImportOutcomeVerificationFailed("该普通导入命令已有不可重放终态。")
            batch = uow.imports.lock_batch(actor.school_id, command.batch_id)
            if batch is None or batch.mode != "ordinary_atomic_units":
                raise MasterDataImportStateConflict("普通导入批次不存在于当前学校。")
            units = uow.imports.lock_units(actor.school_id, batch.id)
            active_command_matches = (
                batch.active_idempotency_record_id == record.record_id
                and batch.active_action_code == action_code
            )
            if batch.status == "applying" and batch.active_idempotency_record_id is not None:
                if not active_command_matches:
                    raise MasterDataImportStateConflict("普通导入正在由另一条固定命令执行。")
            recovering_same_command = (
                not acquisition.created and batch.status == "applying" and active_command_matches
            )
            if (
                not acquisition.created
                and batch.status == "applying"
                and not recovering_same_command
            ):
                raise MasterDataImportOutcomeVerificationFailed(
                    "普通导入的活动命令归属证据缺失或不一致。"
                )
            if batch.status not in expected_statuses and not recovering_same_command:
                raise MasterDataImportStateConflict("普通导入批次状态不允许当前动作。")
            if not recovering_same_command:
                self._verify_locked_confirmation_card(batch, units, command)
            elif batch.confirmation_card_sha256 != command.confirmation_card_sha256:
                raise MasterDataImportVersionConflict("确认卡已经变化，请重新预览。")
            if not recovering_same_command and batch.version != command.expected_batch_version:
                raise MasterDataImportVersionConflict("确认卡已经变化，请重新预览。")
            units_by_sequence = {unit.sequence: unit for unit in units}
            if recovering_same_command:
                if not decisions or not set(decisions).issubset(units_by_sequence):
                    raise MasterDataImportStateConflict("恢复命令引用的原子单元已经变化。")
                for sequence in sorted(decisions):
                    unit = units_by_sequence[sequence]
                    if unit.last_attempt_idempotency_record_id != record.record_id:
                        raise MasterDataImportOutcomeVerificationFailed(
                            "普通导入单元不属于当前恢复命令。"
                        )
                    if unit.last_attempt_completed is False and unit.status in eligible_statuses:
                        to_execute.append(sequence)
                    elif unit.last_attempt_completed is True and unit.status in {
                        "applied",
                        "failed",
                    }:
                        pass
                    else:
                        raise MasterDataImportStateConflict(
                            "恢复命令遇到无法证明归属的原子单元状态。"
                        )
            else:
                eligible = tuple(unit for unit in units if unit.status in eligible_statuses)
                eligible_sequences = {unit.sequence for unit in eligible}
                if set(decisions) != eligible_sequences:
                    raise MasterDataImportStateConflict("人工决定必须逐项覆盖本次可执行单元。")
                for unit in eligible:
                    decision = decisions[unit.sequence]
                    if unit.soft_anomalies and decision.continue_reason is None:
                        raise MasterDataImportStateConflict("同名等软异常必须由人工填写继续理由。")
                    unit.last_attempt_idempotency_record_id = record.record_id
                    unit.last_attempt_completed = False
                    uow.imports.save_unit(unit)
                    to_execute.append(unit.sequence)
            if not recovering_same_command:
                if batch.active_idempotency_record_id is not None:
                    raise MasterDataImportStateConflict("普通导入批次残留另一条活动命令。")
                batch.active_idempotency_record_id = record.record_id
                batch.active_action_code = action_code
                if batch.status != "applying":
                    batch.status = "applying"
                    if all(
                        value is None
                        for value in (
                            batch.confirmed_by_user_id,
                            batch.confirmed_by_person_id,
                            batch.confirmed_by_assignment_id,
                            batch.confirmed_at,
                        )
                    ):
                        batch.confirmed_by_user_id = actor.account_user_id
                        batch.confirmed_by_person_id = actor.person_id
                        batch.confirmed_by_assignment_id = actor.assignment_id
                        batch.confirmed_at = now
                    batch.version += 1
                uow.imports.save_batch(batch)
            if acquisition.created:
                uow.commit()

        for sequence in to_execute:
            self._apply_ordinary_person_unit(
                batch_id=command.batch_id,
                decision=decisions[sequence],
                context=context,
                eligible_statuses=eligible_statuses,
                idempotency_record_id=record.record_id,
            )
        return self._finalize_ordinary(
            command=command,
            context=context,
            actor=actor,
            identity=identity,
            action_code=action_code,
            selected_sequences=frozenset(decisions),
            accepted_attempt_record_ids=frozenset({record.record_id}),
        )

    def _finalize_ordinary(
        self,
        *,
        command: ConfirmImportBatchCommand | TakeOverOrdinaryImportCommand,
        context: CommandExecutionContext,
        actor: StaffActorContext,
        identity: CommandIdempotencyIdentity,
        action_code: str,
        selected_sequences: frozenset[int],
        accepted_attempt_record_ids: frozenset[UUID],
    ) -> ImportApplyOutcome:
        """以当前活动命令和完整 attempt lineage 生成一次不可变普通导入结果。"""

        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()
            self._authorize(uow, actor, now)
            record = uow.idempotency.acquire(identity)
            if record.status == "succeeded":
                return self._verified_replay_outcome(
                    uow.imports,
                    record,
                    identity,
                    action_code=action_code,
                )
            if record.status != "in_progress":
                raise MasterDataImportOutcomeVerificationFailed("普通导入命令已有不可重放终态。")
            batch = uow.imports.lock_batch(actor.school_id, command.batch_id)
            if (
                batch is None
                or batch.status != "applying"
                or batch.active_idempotency_record_id != record.record_id
                or batch.active_action_code != action_code
            ):
                raise MasterDataImportOutcomeVerificationFailed(
                    "普通导入最终结果不属于当前活动命令。"
                )
            units = uow.imports.lock_units(actor.school_id, batch.id)
            verified_selected_sequences = self._verified_ordinary_selected_sequences(
                recovery_reader=uow.imports,
                batch=batch,
                units=units,
                accepted_attempt_record_ids=accepted_attempt_record_ids,
            )
            if verified_selected_sequences != selected_sequences:
                raise MasterDataImportOutcomeVerificationFailed(
                    "锁内复核的普通导入 lineage 与待终结单元集合不一致。"
                )
            selected = {
                unit.sequence: unit for unit in units if unit.sequence in selected_sequences
            }
            if set(selected) != set(selected_sequences):
                raise MasterDataImportStateConflict("最终结果引用的原子单元已经变化。")
            if any(
                unit.last_attempt_idempotency_record_id not in accepted_attempt_record_ids
                or unit.last_attempt_completed is not True
                for unit in selected.values()
            ):
                raise MasterDataImportOutcomeVerificationFailed(
                    "普通导入仍有未完成或归属不明的原子单元。"
                )
            succeeded = tuple(
                sorted(sequence for sequence, unit in selected.items() if unit.status == "applied")
            )
            failed = tuple(
                sorted(sequence for sequence, unit in selected.items() if unit.status == "failed")
            )
            if set(succeeded).union(failed) != set(selected_sequences):
                raise MasterDataImportStateConflict("普通导入仍有未收敛的原子单元。")
            final_status: Literal["partially_failed", "applied"] = (
                "partially_failed" if batch.failed_units else "applied"
            )
            batch.status = final_status
            batch.active_idempotency_record_id = None
            batch.active_action_code = None
            batch.confirmation_card_sha256 = self._card_hash_for_models(batch.id, units)
            batch.version += 1
            uow.imports.save_batch(batch)
            outcome = ImportApplyOutcome(
                batch_id=batch.id,
                status=final_status,
                batch_version=batch.version,
                succeeded_units=succeeded,
                failed_units=failed,
                executed_units=tuple(sorted(selected_sequences)),
            )
            self._record_success_outcome(
                uow,
                record,
                identity,
                outcome,
                action_code=action_code,
            )
            uow.commit()
        return outcome

    @staticmethod
    def _converged_unit_result(
        unit: MasterDataImportUnit,
        idempotency_record_id: UUID,
    ) -> bool | None:
        """只把同一幂等记录已经完成的单元终态收敛为首次布尔结果。"""

        if unit.last_attempt_idempotency_record_id != idempotency_record_id:
            return None
        if unit.last_attempt_completed is not True:
            return None
        if unit.status == "applied":
            return True
        if unit.status == "failed":
            return False
        raise MasterDataImportOutcomeVerificationFailed("已完成 attempt 与单元终态不一致。")

    def _apply_ordinary_person_unit(
        self,
        *,
        batch_id: UUID,
        decision: ImportUnitDecision,
        context: CommandExecutionContext,
        eligible_statuses: tuple[str, ...],
        idempotency_record_id: UUID,
    ) -> bool:
        actor = self._staff_actor(context)
        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()
            self._authorize(uow, actor, now)
            batch = uow.imports.lock_batch(actor.school_id, batch_id)
            if batch is None:
                raise MasterDataImportStateConflict("普通导入批次不存在于当前学校。")
            unit = uow.imports.lock_unit(actor.school_id, batch.id, decision.sequence)
            if unit is None:
                raise MasterDataImportStateConflict("普通导入原子单元不存在于当前批次。")
            converged = self._converged_unit_result(unit, idempotency_record_id)
            if converged is not None:
                return converged
            if batch.status != "applying":
                raise MasterDataImportStateConflict("普通导入批次不在应用状态。")
            if unit.status not in eligible_statuses:
                raise MasterDataImportStateConflict("原子单元已经被其他执行者处理。")
            if (
                batch.active_idempotency_record_id != idempotency_record_id
                or unit.last_attempt_idempotency_record_id != idempotency_record_id
                or unit.last_attempt_completed is not False
            ):
                raise MasterDataImportOutcomeVerificationFailed("原子单元不属于当前未完成命令。")
            if self._json_hash(unit.payload) != unit.payload_sha256:
                raise MasterDataImportVersionConflict("普通导入单元载荷摘要已经变化。")
            previous_status = unit.status
            if unit.version != decision.expected_unit_version:
                self._mark_unit_failed(
                    batch,
                    unit,
                    previous_status=previous_status,
                    error_code="unit_version_conflict",
                )
                unit.last_attempt_completed = True
                uow.imports.save_unit(unit)
                uow.imports.save_batch(batch)
                uow.commit()
                return False
            external_reference = str(unit.payload["external_reference"])
            if uow.imports.external_reference_exists(actor.school_id, external_reference):
                self._mark_unit_failed(
                    batch,
                    unit,
                    previous_status=previous_status,
                    error_code="external_reference_conflict",
                    error_field_code="校内编号",
                )
                unit.last_attempt_completed = True
                uow.imports.save_unit(unit)
                uow.imports.save_batch(batch)
                uow.commit()
                return False
            display_name = str(unit.payload["display_name"])
            uow.imports.lock_display_name_candidate(actor.school_id, display_name)
            if uow.imports.same_display_name_exists(actor.school_id, display_name) and not any(
                anomaly.get("code") == "same_display_name" for anomaly in unit.soft_anomalies
            ):
                unit.soft_anomalies = [*unit.soft_anomalies, {"code": "same_display_name"}]
                self._mark_unit_failed(
                    batch,
                    unit,
                    previous_status=previous_status,
                    error_code="soft_anomaly_changed",
                    error_field_code="教职工姓名",
                )
                unit.last_attempt_completed = True
                uow.imports.save_unit(unit)
                uow.imports.save_batch(batch)
                uow.commit()
                return False
            person_id = uuid5(batch.id, f"person:{unit.id}")
            self._add_person_from_unit(
                uow,
                actor=actor,
                unit=unit,
                person_id=person_id,
            )
            if previous_status == "failed":
                batch.failed_units -= 1
            else:
                batch.pending_units -= 1
            batch.succeeded_units += 1
            batch.version += 1
            unit.status = "applied"
            unit.decision_reason = decision.continue_reason
            unit.target_id = person_id
            unit.target_version = 1
            unit.error_code = None
            unit.error_field_code = None
            unit.last_attempt_completed = True
            unit.version += 1
            uow.imports.save_unit(unit)
            uow.imports.save_batch(batch)
            uow.audit.append_person_imported(
                batch_id=batch.id,
                unit_id=unit.id,
                person_id=person_id,
                source_kind=batch.source_kind,
                continued_same_name=any(
                    anomaly.get("code") == "same_display_name" for anomaly in unit.soft_anomalies
                ),
            )
            uow.outbox.append(
                OutboxEvent(
                    event_id=uuid5(person_id, "outbox:imported"),
                    school_id=SchoolId(actor.school_id),
                    event_key=f"master-data.person-imported:{person_id}",
                    event_type="master_data.person.imported",
                    aggregate_type="school_person",
                    aggregate_id=person_id,
                    aggregate_version=1,
                    occurred_at=now,
                )
            )
            uow.commit()
            return True

    def _record_unit_transaction_failure(
        self,
        *,
        batch_id: UUID,
        sequence: int,
        context: CommandExecutionContext,
        eligible_statuses: tuple[str, ...],
        idempotency_record_id: UUID,
    ) -> None:
        """正式单元事务回滚后，用新事务只记录结构化失败以支持精确续跑。"""

        actor = self._staff_actor(context)
        with self._uow_factory.open(context) as uow:
            now = uow.current_database_time()
            self._authorize(uow, actor, now)
            batch = uow.imports.lock_batch(actor.school_id, batch_id)
            if batch is None:
                raise MasterDataImportStateConflict("失败记录时批次不存在于当前学校。")
            unit = uow.imports.lock_unit(actor.school_id, batch.id, sequence)
            if unit is None:
                raise MasterDataImportStateConflict("失败记录时原子单元不存在于当前批次。")
            if self._converged_unit_result(unit, idempotency_record_id) is not None:
                return
            if batch.status != "applying":
                raise MasterDataImportStateConflict("失败记录时批次已不在应用状态。")
            if unit.status not in eligible_statuses:
                raise MasterDataImportStateConflict("失败记录时原子单元状态已经变化。")
            if (
                batch.active_idempotency_record_id != idempotency_record_id
                or unit.last_attempt_idempotency_record_id != idempotency_record_id
                or unit.last_attempt_completed is not False
            ):
                raise MasterDataImportOutcomeVerificationFailed("失败记录不属于当前未完成命令。")
            self._mark_unit_failed(
                batch,
                unit,
                previous_status=unit.status,
                error_code="unit_transaction_failed",
            )
            unit.last_attempt_completed = True
            uow.imports.save_unit(unit)
            uow.imports.save_batch(batch)
            uow.commit()

    @staticmethod
    def _mark_unit_failed(
        batch: MasterDataImportBatch,
        unit: MasterDataImportUnit,
        *,
        previous_status: str,
        error_code: str,
        error_field_code: str | None = None,
    ) -> None:
        if previous_status != "failed":
            batch.pending_units -= 1
            batch.failed_units += 1
        batch.version += 1
        unit.status = "failed"
        unit.error_code = error_code
        unit.error_field_code = error_field_code
        unit.version += 1

    def _add_person_from_unit(
        self,
        uow: MasterDataImportUoW,
        *,
        actor: StaffActorContext,
        unit: MasterDataImportUnit,
        person_id: UUID,
    ) -> None:
        payload = unit.payload
        person = SchoolPerson(
            id=person_id,
            school_id=actor.school_id,
            status="active",
            version=1,
        )
        name = SchoolPersonName(
            id=uuid5(person_id, "name:1"),
            school_id=actor.school_id,
            person_id=person_id,
            display_name=str(payload["display_name"]),
            valid_from=datetime.fromisoformat(str(payload["valid_from"])),
            valid_to=None,
            source_kind="person_external_reference",
            source_ref=str(payload["external_reference"]),
            status="confirmed",
            version=1,
        )
        uow.people.add_person(person)
        uow.people.add_name(name)

    def _authorize(
        self,
        uow: MasterDataImportUoW,
        actor: StaffActorContext,
        now: datetime,
    ) -> None:
        locked = uow.assignments.lock_assignment(actor.school_id, actor.assignment_id)
        if (
            locked is None
            or locked.person_id != actor.person_id
            or locked.version != actor.assignment_version
            or locked.status != "confirmed"
            or locked.interval.valid_from > now
            or (locked.interval.valid_to is not None and locked.interval.valid_to <= now)
        ):
            raise MasterDataImportContextRequired("当前教职工任职已经失效或变化。")
        if not self._authorizer.is_allowed(locked.semantic_role_code):
            raise MasterDataImportPermissionDenied("当前任职没有主数据导入权限。")

    @staticmethod
    def _staff_actor(context: CommandExecutionContext) -> StaffActorContext:
        actor = context.actor
        if type(actor) is not StaffActorContext:
            raise MasterDataImportContextRequired("主数据导入必须明确选择当前教职工任职。")
        return actor

    @staticmethod
    def _operation_id(context: CommandExecutionContext, *, action: str) -> UUID:
        actor = context.actor
        if type(actor) is not StaffActorContext:
            raise MasterDataImportContextRequired("主数据导入必须绑定教职工任职。")
        key_hash = sha256(context.idempotency_key.encode("utf-8")).hexdigest()
        return uuid5(
            _ID_NAMESPACE,
            "\x1f".join(
                (
                    str(actor.school_id),
                    str(actor.assignment_id),
                    str(actor.assignment_version),
                    action,
                    key_hash,
                )
            ),
        )

    @staticmethod
    def _idempotency_identity(
        command: ConfirmImportBatchCommand,
        context: CommandExecutionContext,
        actor: StaffActorContext,
        *,
        action_code: str,
    ) -> CommandIdempotencyIdentity:
        """把确认动作、操作者、批次和规范请求绑定为共享幂等身份。"""

        request_json = json.dumps(
            {
                "schema": "master_data_import_confirmation.v1",
                "action_code": action_code,
                "command": command.model_dump(mode="json"),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return CommandIdempotencyIdentity(
            school_id=actor.school_id,
            action_code=action_code,
            actor_user_id=actor.account_user_id,
            actor_person_id=actor.person_id,
            actor_context_type="assignment",
            actor_assignment_id=actor.assignment_id,
            actor_assignment_version=actor.assignment_version,
            resource_type=_IDEMPOTENCY_RESOURCE_TYPE,
            resource_id=str(command.batch_id),
            idempotency_key_hash=sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
            request_sha256=sha256(request_json.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _takeover_idempotency_identity(
        command: TakeOverOrdinaryImportCommand,
        context: CommandExecutionContext,
        actor: StaffActorContext,
    ) -> CommandIdempotencyIdentity:
        """把接管动作、当前任职、批次和规范请求绑定为新的共享幂等身份。"""

        action_code = "master_data.import.ordinary_takeover"
        request_json = json.dumps(
            {
                "schema": "master_data_import_takeover.v1",
                "action_code": action_code,
                "command": command.model_dump(mode="json"),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return CommandIdempotencyIdentity(
            school_id=actor.school_id,
            action_code=action_code,
            actor_user_id=actor.account_user_id,
            actor_person_id=actor.person_id,
            actor_context_type="assignment",
            actor_assignment_id=actor.assignment_id,
            actor_assignment_version=actor.assignment_version,
            resource_type=_IDEMPOTENCY_RESOURCE_TYPE,
            resource_id=str(command.batch_id),
            idempotency_key_hash=sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
            request_sha256=sha256(request_json.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _validated_outcome_sequences(
        outcome: MasterDataImportCommandOutcome | SucceededImportAttemptOutcome,
    ) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
        """返回排序且互斥的首次结果集合；任何重复、缺口或额外序号均拒绝。"""

        succeeded = tuple(outcome.succeeded_units)
        failed = tuple(outcome.failed_units)
        executed = tuple(outcome.executed_units)
        if (
            tuple(sorted(set(succeeded))) != succeeded
            or tuple(sorted(set(failed))) != failed
            or tuple(sorted(set(executed))) != executed
            or set(succeeded).intersection(failed)
            or set(succeeded).union(failed) != set(executed)
        ):
            raise MasterDataImportOutcomeVerificationFailed("不可变导入结果的单元集合无效。")
        return succeeded, failed, executed

    @staticmethod
    def _verified_replay_outcome(
        reader: MasterDataImportOutcomeReader,
        record: IdempotencyRecordProjection,
        identity: CommandIdempotencyIdentity,
        *,
        action_code: str,
    ) -> ImportApplyOutcome:
        """交叉核对共享幂等终态和不可变序号结果后返回首次结果。"""

        result = record.result
        outcome = reader.find_outcome(identity.school_id, record.record_id)
        if (
            record.status != "succeeded"
            or record.result_type != _IDEMPOTENCY_RESULT_TYPE
            or result is None
            or result.resource_type != _IDEMPOTENCY_RESOURCE_TYPE
            or result.resource_id != UUID(identity.resource_id)
            or outcome is None
            or outcome.batch_id != result.resource_id
            or outcome.action_code != action_code
            or outcome.batch_version != result.resource_version
            or outcome.status != result.outcome_code
        ):
            raise MasterDataImportOutcomeVerificationFailed("幂等终态与不可变导入结果不一致。")
        succeeded, failed, executed = MasterDataImportService._validated_outcome_sequences(outcome)
        return ImportApplyOutcome(
            batch_id=outcome.batch_id,
            status=cast(Literal["partially_failed", "applied", "published"], outcome.status),
            batch_version=outcome.batch_version,
            succeeded_units=succeeded,
            failed_units=failed,
            executed_units=executed,
        )

    @staticmethod
    def _record_success_outcome(
        uow: MasterDataImportUoW,
        record: IdempotencyRecordProjection,
        identity: CommandIdempotencyIdentity,
        outcome: ImportApplyOutcome,
        *,
        action_code: str,
    ) -> None:
        """在业务事务内写不可变精确结果并把共享幂等记录转为成功。"""

        stored = MasterDataImportCommandOutcome(
            id=uuid5(record.record_id, "master-data-import-outcome"),
            school_id=identity.school_id,
            idempotency_record_id=record.record_id,
            batch_id=outcome.batch_id,
            action_code=action_code,
            status=outcome.status,
            batch_version=outcome.batch_version,
            succeeded_units=list(outcome.succeeded_units),
            failed_units=list(outcome.failed_units),
            executed_units=list(outcome.executed_units),
        )
        uow.imports.add_outcome(stored)
        uow.idempotency.mark_succeeded(
            identity,
            result_type=_IDEMPOTENCY_RESULT_TYPE,
            result=SafeCommandResult(
                resource_type=_IDEMPOTENCY_RESOURCE_TYPE,
                resource_id=outcome.batch_id,
                resource_version=outcome.batch_version,
                outcome_code=outcome.status,
            ),
        )

    @staticmethod
    def _verified_takeover_attempt_lineage(
        reader: MasterDataImportRecoveryReader,
        *,
        school_id: SchoolId,
        batch_id: UUID,
        new_record_id: UUID,
    ) -> frozenset[UUID]:
        """交叉证明 root confirm/resume→takeover 的每条不可变 record 边。"""

        lineage = reader.find_attempt_lineage(school_id, new_record_id)
        if not lineage or len(set(lineage)) != len(lineage) or new_record_id in lineage:
            raise MasterDataImportOutcomeVerificationFailed(
                "接管 attempt lineage 缺失、重复或形成环。"
            )
        current_edge = reader.find_takeover_by_new_record(school_id, new_record_id)
        if (
            current_edge is None
            or current_edge.batch_id != batch_id
            or lineage[-1] != current_edge.previous_idempotency_record_id
        ):
            raise MasterDataImportOutcomeVerificationFailed(
                "接管 attempt lineage 没有精确连接当前 takeover。"
            )
        return frozenset((*lineage, new_record_id))

    def _probe_takeover_commit(
        self,
        command: TakeOverOrdinaryImportCommand,
        *,
        context: CommandExecutionContext,
        identity: CommandIdempotencyIdentity,
        allow_absent: bool,
    ) -> TakeoverCommitCheckpoint | ImportApplyOutcome | None:
        """在新物理只读事务中区分可靠 checkpoint、精确终态和证据不足。"""

        action_code = "master_data.import.ordinary_takeover"
        command_manifest = {unit.sequence: unit.expected_unit_version for unit in command.units}
        with self._uow_factory.open_probe(context) as uow:
            record = uow.idempotency.probe(identity)
            if record is None:
                if allow_absent:
                    return None
                raise MasterDataImportOutcomeVerificationFailed(
                    "接管提交结果未知且没有找到固定幂等记录。"
                )
            takeover = uow.recoveries.find_takeover_by_new_record(
                identity.school_id,
                record.record_id,
            )
            batch = uow.recoveries.find_batch(identity.school_id, command.batch_id)
            units = uow.recoveries.find_units(identity.school_id, command.batch_id)
            if (
                takeover is None
                or batch is None
                or takeover.batch_id != command.batch_id
                or takeover.new_idempotency_record_id != record.record_id
                or takeover.batch_version_after != takeover.batch_version_before + 1
                or tuple(sorted(takeover.transferred_sequences))
                != tuple(takeover.transferred_sequences)
                or len(takeover.transferred_sequences) != len(takeover.transferred_unit_versions)
                or dict(
                    zip(
                        takeover.transferred_sequences,
                        takeover.transferred_unit_versions,
                        strict=True,
                    )
                )
                != command_manifest
            ):
                raise MasterDataImportOutcomeVerificationFailed(
                    "接管提交结果未知且 takeover、manifest 或批次证据不完整。"
                )
            accepted_attempt_ids = self._verified_takeover_attempt_lineage(
                uow.recoveries,
                school_id=identity.school_id,
                batch_id=command.batch_id,
                new_record_id=record.record_id,
            )
            units_by_sequence = {unit.sequence: unit for unit in units}
            if len(units_by_sequence) != batch.planned_units:
                raise MasterDataImportOutcomeVerificationFailed(
                    "接管提交结果未知且批次单元数量不完整。"
                )
            selected_sequences = self._verified_ordinary_selected_sequences(
                recovery_reader=uow.recoveries,
                batch=batch,
                units=units,
                accepted_attempt_record_ids=accepted_attempt_ids,
            )

            if record.status == "in_progress":
                if (
                    record.result_type is not None
                    or record.result is not None
                    or batch.mode != "ordinary_atomic_units"
                    or batch.status != "applying"
                    or batch.active_idempotency_record_id != record.record_id
                    or batch.active_action_code != action_code
                ):
                    raise MasterDataImportOutcomeVerificationFailed(
                        "接管提交结果未知且活动 checkpoint 形态不完整。"
                    )
                self._verified_takeover_manifest_progress(
                    batch=batch,
                    takeover=takeover,
                    units_by_sequence=units_by_sequence,
                    command_decisions={
                        unit.sequence: (unit.expected_unit_version, unit.continue_reason)
                        for unit in command.units
                    },
                    new_record_id=record.record_id,
                )
                if not set(command_manifest).issubset(selected_sequences):
                    raise MasterDataImportOutcomeVerificationFailed(
                        "接管提交结果未知且 manifest 不属于 accepted lineage。"
                    )
                if any(
                    unit.last_attempt_idempotency_record_id in accepted_attempt_ids
                    and sequence not in command_manifest
                    and (
                        unit.last_attempt_completed is not True
                        or unit.status not in {"applied", "failed"}
                    )
                    for sequence, unit in units_by_sequence.items()
                ):
                    raise MasterDataImportOutcomeVerificationFailed(
                        "接管提交结果未知且 predecessor 单元事实不完整。"
                    )
                return TakeoverCommitCheckpoint(
                    status="takeover_committed",
                    batch_id=command.batch_id,
                    takeover_id=takeover.id,
                    batch_version_after=takeover.batch_version_after,
                    new_idempotency_record_id=record.record_id,
                    transferred_sequences=tuple(takeover.transferred_sequences),
                )

            if record.status == "succeeded":
                outcome = self._verified_replay_outcome(
                    uow.outcomes,
                    record,
                    identity,
                    action_code=action_code,
                )
                if (
                    batch.status != outcome.status
                    or batch.version != outcome.batch_version
                    or batch.active_idempotency_record_id is not None
                    or batch.active_action_code is not None
                ):
                    raise MasterDataImportOutcomeVerificationFailed(
                        "接管首次结果与当前批次终态不一致。"
                    )
                lineage_units = {
                    sequence: unit
                    for sequence, unit in units_by_sequence.items()
                    if sequence in selected_sequences
                }
                if set(lineage_units) != set(outcome.executed_units) or any(
                    unit.last_attempt_completed is not True
                    or unit.status not in {"applied", "failed"}
                    for unit in lineage_units.values()
                ):
                    raise MasterDataImportOutcomeVerificationFailed(
                        "接管首次结果没有完整覆盖 accepted lineage 单元。"
                    )
                if {
                    sequence for sequence, unit in lineage_units.items() if unit.status == "applied"
                } != set(outcome.succeeded_units) or {
                    sequence for sequence, unit in lineage_units.items() if unit.status == "failed"
                } != set(outcome.failed_units):
                    raise MasterDataImportOutcomeVerificationFailed(
                        "接管首次结果与 lineage 单元终态集合不一致。"
                    )
                return outcome

            raise MasterDataImportOutcomeVerificationFailed(
                "接管提交结果未知且固定幂等记录不是可恢复状态。"
            )

    def _probe_outcome(
        self,
        context: CommandExecutionContext,
        identity: CommandIdempotencyIdentity,
        *,
        action_code: str,
    ) -> ImportApplyOutcome:
        """提交结果未知时只在新物理只读事务中探测首次安全结果。"""

        with self._uow_factory.open_probe(context) as uow:
            record = uow.idempotency.probe(identity)
            if record is None or record.status != "succeeded":
                raise MasterDataImportOutcomeVerificationFailed(
                    "提交结果未知且没有找到已成功的固定幂等终态。"
                )
            return self._verified_replay_outcome(
                uow.outcomes,
                record,
                identity,
                action_code=action_code,
            )

    @staticmethod
    def _json_hash(value: object) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _card_hash(
        cls,
        batch_id: UUID,
        units: tuple[tuple[int, int, str, tuple[str, ...]], ...],
    ) -> str:
        return cls._json_hash(
            {
                "schema": "master_data_import_confirmation.v1",
                "batch_id": str(batch_id),
                "units": units,
            }
        )

    @classmethod
    def _card_hash_for_models(
        cls,
        batch_id: UUID,
        units: tuple[MasterDataImportUnit, ...],
    ) -> str:
        return cls._card_hash(
            batch_id,
            tuple(
                (
                    unit.sequence,
                    unit.version,
                    unit.payload_sha256,
                    tuple(str(anomaly["code"]) for anomaly in unit.soft_anomalies),
                )
                for unit in units
            ),
        )

    @classmethod
    def _verify_locked_confirmation_card(
        cls,
        batch: MasterDataImportBatch,
        units: tuple[MasterDataImportUnit, ...],
        command: ConfirmImportBatchCommand | TakeOverOrdinaryImportCommand,
    ) -> None:
        """从已锁定事实重算载荷摘要和确认卡，拒绝存量摘要被绕过。"""

        if any(cls._json_hash(unit.payload) != unit.payload_sha256 for unit in units):
            raise MasterDataImportVersionConflict("导入单元载荷摘要已经变化。")
        recalculated = cls._card_hash_for_models(batch.id, units)
        if (
            batch.confirmation_card_sha256 != recalculated
            or command.confirmation_card_sha256 != recalculated
        ):
            raise MasterDataImportVersionConflict("确认卡事实已经变化，请重新预览。")

    @classmethod
    def _verify_locked_takeover_confirmation_card(
        cls,
        batch: MasterDataImportBatch,
        units: tuple[MasterDataImportUnit, ...],
        command: TakeOverOrdinaryImportCommand,
    ) -> None:
        """接管时保留首次确认卡，并重验所有已锁定载荷。

        普通导入单元完成后版本会推进，因此不能用当前全量单元重算首次确认卡。
        尚未完成单元的 sequence/version 由接管命令另行精确绑定。
        """

        if any(cls._json_hash(unit.payload) != unit.payload_sha256 for unit in units):
            raise MasterDataImportVersionConflict("导入单元载荷摘要已经变化。")
        if (
            batch.confirmation_card_sha256 is None
            or command.confirmation_card_sha256 != batch.confirmation_card_sha256
        ):
            raise MasterDataImportVersionConflict("确认卡事实已经变化，请重新预览。")

    @classmethod
    def _preview(
        cls,
        batch: MasterDataImportBatch,
        units: tuple[MasterDataImportUnit, ...],
    ) -> ImportBatchPreview:
        previews: list[ImportUnitPreview] = []
        for unit in units:
            previews.append(
                ImportUnitPreview(
                    sequence=unit.sequence,
                    row_number=unit.row_number,
                    unit_version=unit.version,
                    payload_sha256=unit.payload_sha256,
                    soft_anomaly_codes=cls._soft_anomaly_codes(unit.soft_anomalies),
                )
            )
        if batch.confirmation_card_sha256 is None:
            raise MasterDataImportStateConflict("该批次还没有可确认预览。")
        return ImportBatchPreview(
            batch_id=batch.id,
            mode=cast(ImportMode, batch.mode),
            status=cast(
                Literal[
                    "mapped",
                    "preflight_failed",
                    "ready_for_confirmation",
                    "applying",
                    "partially_failed",
                    "applied",
                    "published",
                ],
                batch.status,
            ),
            batch_version=batch.version,
            confirmation_card_sha256=batch.confirmation_card_sha256,
            units=tuple(previews),
        )

    @classmethod
    def _stage_replay_preview(
        cls,
        batch: MasterDataImportBatch,
        units: tuple[MasterDataImportUnit, ...],
    ) -> ImportBatchPreview:
        """重建首次登记响应，避免后续状态变化改写同键重放语义。"""

        initial_status_by_mode: dict[str, Literal["mapped", "ready_for_confirmation"]] = {
            "manual_unit": "ready_for_confirmation",
            "ordinary_atomic_units": "mapped",
            "authoritative_set_publish": "ready_for_confirmation",
        }
        initial_status = initial_status_by_mode.get(batch.mode)
        if initial_status is None:
            raise MasterDataImportStateConflict("导入批次包含未知模式。")
        initial_units = tuple(
            ImportUnitPreview(
                sequence=unit.sequence,
                row_number=unit.row_number,
                unit_version=1,
                payload_sha256=unit.payload_sha256,
                soft_anomaly_codes=cls._soft_anomaly_codes(unit.initial_soft_anomalies),
            )
            for unit in units
        )
        return ImportBatchPreview(
            batch_id=batch.id,
            mode=cast(ImportMode, batch.mode),
            status=initial_status,
            batch_version=1,
            confirmation_card_sha256=cls._card_hash(
                batch.id,
                tuple(
                    (
                        unit.sequence,
                        1,
                        unit.payload_sha256,
                        cls._soft_anomaly_codes(unit.initial_soft_anomalies),
                    )
                    for unit in units
                ),
            ),
            units=initial_units,
        )

    @staticmethod
    def _soft_anomaly_codes(
        anomalies: list[dict[str, object]],
    ) -> tuple[Literal["same_display_name"], ...]:
        codes: list[Literal["same_display_name"]] = []
        for anomaly in anomalies:
            if anomaly.get("code") != "same_display_name":
                raise MasterDataImportStateConflict("导入单元包含未知软异常代码。")
            codes.append("same_display_name")
        return tuple(codes)
