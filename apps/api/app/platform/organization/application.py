# pyright: strict

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from types import TracebackType
from typing import Literal, NoReturn, Protocol, Self
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.db.rls_context import DatabaseExecutionContextEnvelope
from app.db.uow import CommitOutcomeUnknown, SqlAlchemyUnitOfWork
from app.platform.audit.contracts import (
    BusinessAuditRecord,
    BusinessAuditSummary,
    TypedAuditWriter,
)
from app.platform.audit.service import BoundTypedAuditWriter
from app.platform.contracts import (
    AssignmentId,
    CommandExecutionContext,
    EffectiveInterval,
    SchoolId,
    SemanticCode,
    StaffActorContext,
)
from app.platform.directory.ports import PersonVersionLockPort
from app.platform.directory.repository import SqlAlchemyPersonRepository
from app.platform.idempotency.contracts import (
    CommandIdempotencyIdentity,
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
from app.platform.messaging.contracts import OutboxEvent
from app.platform.messaging.ports import (
    StaffAssignmentOutboxProbe,
    StaffAssignmentOutboxTransactionPort,
)
from app.platform.messaging.repository import (
    SqlAlchemyOutboxWriter,
    SqlAlchemyStaffAssignmentOutboxProbe,
)
from app.platform.organization.contracts import (
    ConfirmStaffDutyBindingCommand,
    ConfirmStaffDutyBindingOutcome,
    NewConfirmedStaffAssignment,
    StaffDutyBindingAuthorizationFacts,
    StaffDutyBindingAuthorizer,
)
from app.platform.organization.ports import (
    ConfirmedStaffAssignmentReader,
    StaffAssignmentMutationPort,
)
from app.platform.organization.repository import (
    SqlAlchemyConfirmedStaffAssignmentReader,
    SqlAlchemyOrganizationRepository,
)
from app.platform.responsibility.contracts import NewConfirmedResponsibilityMembership
from app.platform.responsibility.ports import (
    ConfirmedResponsibilityMembershipReader,
    ResponsibilityMembershipMutationPort,
)
from app.platform.responsibility.repository import (
    SqlAlchemyConfirmedResponsibilityMembershipReader,
    SqlAlchemyResponsibilityRepository,
)

_ACTION_CODE: SemanticCode = "master_data.staff_duty_binding.confirm"
_IDEMPOTENCY_RESOURCE_TYPE: SemanticCode = "staff_duty_binding"
_RESULT_TYPE: SemanticCode = "master_data.staff_duty_binding.result"
_ASSIGNMENT_RESOURCE_TYPE: SemanticCode = "staff_assignment"
_ID_NAMESPACE = UUID("8bcdbf21-45da-51ea-9c0b-13272f025bd1")
_SEMANTIC_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")


class StaffDutyBindingError(RuntimeError):
    """所有正式任职—责任组确认失败的稳定应用错误基类。"""


class StaffDutyBindingLegacyWriterDisabled(StaffDutyBindingError):
    """旧任职—责任组连带写入口已禁用，等待正式 Canonical 任职写入重建。"""


class StaffDutyBindingContextRequired(StaffDutyBindingError):
    """表示该动作没有使用当前有效的教职工任职上下文。"""


class StaffDutyBindingPermissionDenied(StaffDutyBindingError):
    """表示服务端授权策略拒绝当前任职执行该动作。"""


class StaffDutyBindingDutySeparationRequired(StaffDutyBindingError):
    """表示受益人试图批准或授予自己的正式任职。"""


class StaffDutyBindingTargetNotFound(StaffDutyBindingError):
    """表示确认卡引用不属于当前学校或已不能参与新业务。"""


class StaffDutyBindingVersionConflict(StaffDutyBindingError):
    """表示确认卡绑定的人员、组织或责任组版本已经变化。"""


class StaffDutyBindingRetryableConflict(StaffDutyBindingError):
    """表示数据库锁超时；调用方可重新预览后人工重试，Handler 不自动重试。"""


class StaffDutyBindingAssignmentWriteFailed(StaffDutyBindingError):
    """表示正式任职写入被数据库约束拒绝。"""


class StaffDutyBindingMembershipWriteFailed(StaffDutyBindingError):
    """表示责任组成员写入被数据库约束拒绝。"""


class StaffDutyBindingAuditWriteFailed(StaffDutyBindingError):
    """表示类型化业务审计未能加入同一事务。"""


class StaffDutyBindingOutboxWriteFailed(StaffDutyBindingError):
    """表示安全 Outbox 事件未能加入同一事务。"""


class StaffDutyBindingIdempotencyWriteFailed(StaffDutyBindingError):
    """表示成功终态未能写入同一幂等事务。"""


class StaffDutyBindingOutcomeVerificationFailed(StaffDutyBindingError):
    """表示幂等终态与固定权威行不一致，必须失败关闭。"""


class ConfiguredStaffDutyBindingAuthorizer:
    """用服务端加载的语义职责集合授权；没有配置时默认拒绝。"""

    def __init__(self, allowed_semantic_role_codes: frozenset[str]) -> None:
        if any(
            _SEMANTIC_CODE_PATTERN.fullmatch(code) is None for code in allowed_semantic_role_codes
        ):
            raise ValueError("主数据确认授权只能配置合法语义职责代码。")
        self._allowed_semantic_role_codes = allowed_semantic_role_codes

    def is_allowed(self, facts: StaffDutyBindingAuthorizationFacts) -> bool:
        """只信任服务端锁定任职中的语义代码，不读取客户端权限布尔值。"""

        return facts.actor_semantic_role_code in self._allowed_semantic_role_codes


class StaffDutyBindingOutcomeProbe(Protocol):
    """幂等重放与未知提交恢复共同使用的四个固定键 Reader。"""

    @property
    def assignments(self) -> ConfirmedStaffAssignmentReader: ...

    @property
    def responsibility_memberships(self) -> ConfirmedResponsibilityMembershipReader: ...

    @property
    def idempotency(self) -> CommandIdempotencyProbeReader: ...

    @property
    def outbox(self) -> StaffAssignmentOutboxProbe: ...


class StaffDutyBindingCommandUoW(Protocol):
    """只暴露一张确认卡命令事务所需的窄写端口。"""

    people: PersonVersionLockPort
    assignments: StaffAssignmentMutationPort
    responsibility_memberships: ResponsibilityMembershipMutationPort
    idempotency: CommandIdempotencyRepository
    audit: TypedAuditWriter
    outbox: StaffAssignmentOutboxTransactionPort

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def current_database_time(self) -> datetime: ...

    def commit(self) -> None: ...


class StaffDutyBindingProbeUoW(StaffDutyBindingOutcomeProbe, Protocol):
    """物理只读恢复事务；类型和运行时对象都不暴露 commit 或 Writer。"""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class StaffDutyBindingUoWFactory(Protocol):
    """为命令执行和结果探测分别创建全新事务。"""

    def open_command(
        self,
        context: CommandExecutionContext,
    ) -> StaffDutyBindingCommandUoW: ...

    def open_probe(
        self,
        context: CommandExecutionContext,
    ) -> StaffDutyBindingProbeUoW: ...


class SqlAlchemyStaffDutyBindingUoW(SqlAlchemyUnitOfWork):
    """在一个 Session 中组合 Task 7 的六个窄端口。"""

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
        self.people: PersonVersionLockPort
        self.assignments: StaffAssignmentMutationPort
        self.responsibility_memberships: ResponsibilityMembershipMutationPort
        self.idempotency: CommandIdempotencyRepository
        self.audit: TypedAuditWriter
        self.outbox: StaffAssignmentOutboxTransactionPort

    def __enter__(self) -> Self:
        super().__enter__()
        try:
            self.session.execute(
                select(func.set_config("lock_timeout", f"{self._lock_timeout_ms}ms", True))
            )
            self.people = SqlAlchemyPersonRepository(self.session)
            self.assignments = SqlAlchemyOrganizationRepository(self.session)
            self.responsibility_memberships = SqlAlchemyResponsibilityRepository(self.session)
            self.idempotency = SqlAlchemyCommandIdempotencyRepository(self.session)
            self.audit = BoundTypedAuditWriter(self.session, self._context)
            self.outbox = SqlAlchemyOutboxWriter(self.session)
        except BaseException as exc:
            super().__exit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    def current_database_time(self) -> datetime:
        """每次事务只由 Handler 取一次 PostgreSQL 时钟。"""

        current = self.session.scalar(select(func.clock_timestamp()))
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise RuntimeError("PostgreSQL 没有返回带时区的数据库时钟。")
        return current


class SqlAlchemyStaffDutyBindingProbeUoW:
    """组合物理只读 UoW 与四个只读适配器，不继承通用 UoW 写能力。"""

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
        self.assignments: ConfirmedStaffAssignmentReader
        self.responsibility_memberships: ConfirmedResponsibilityMembershipReader
        self.idempotency: CommandIdempotencyProbeReader
        self.outbox: StaffAssignmentOutboxProbe

    def __enter__(self) -> Self:
        self._delegate.__enter__()
        try:
            session = self._delegate.session
            self.assignments = SqlAlchemyConfirmedStaffAssignmentReader(session)
            self.responsibility_memberships = SqlAlchemyConfirmedResponsibilityMembershipReader(
                session
            )
            self.idempotency = SqlAlchemyCommandIdempotencyProbeReader(session)
            self.outbox = SqlAlchemyStaffAssignmentOutboxProbe(session)
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


class SqlAlchemyStaffDutyBindingUoWFactory:
    """从可信 envelope 工厂创建命令或物理只读探测 UoW。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        envelope_factory: Callable[[CommandExecutionContext], DatabaseExecutionContextEnvelope],
        lock_timeout_ms: int = 750,
    ) -> None:
        if not 50 <= lock_timeout_ms <= 5000:
            raise ValueError("Task 7 数据库锁超时必须介于 50ms 与 5000ms。")
        self._session_factory = session_factory
        self._envelope_factory = envelope_factory
        self._lock_timeout_ms = lock_timeout_ms

    def open_command(
        self,
        context: CommandExecutionContext,
    ) -> SqlAlchemyStaffDutyBindingUoW:
        """每次命令调用都新建可写 UoW。"""

        return SqlAlchemyStaffDutyBindingUoW(
            session_factory=self._session_factory,
            database_context_envelope=self._envelope_factory(context),
            context=context,
            lock_timeout_ms=self._lock_timeout_ms,
        )

    def open_probe(
        self,
        context: CommandExecutionContext,
    ) -> SqlAlchemyStaffDutyBindingProbeUoW:
        """每次探测都新建物理 READ ONLY UoW，且不返回任何写能力。"""

        return SqlAlchemyStaffDutyBindingProbeUoW(
            session_factory=self._session_factory,
            database_context_envelope=self._envelope_factory(context),
        )


@dataclass(frozen=True, slots=True)
class _BindingIds:
    """按同一幂等范围确定性生成 assignment、membership 和 outbox ID。"""

    assignment_id: AssignmentId
    membership_id: UUID
    outbox_event_id: UUID


class ConfirmStaffDutyBindingHandler:
    """已机械禁用的旧任职—责任组连带确认 Handler。"""

    def __init__(
        self,
        *,
        uow_factory: StaffDutyBindingUoWFactory,
        authorizer: StaffDutyBindingAuthorizer,
    ) -> None:
        self._uow_factory = uow_factory
        self._authorizer = authorizer

    def handle(
        self,
        command: ConfirmStaffDutyBindingCommand,
        *,
        context: CommandExecutionContext,
    ) -> ConfirmStaffDutyBindingOutcome:
        """拒绝旧稳定任职直写入口；正式任职只能经 School Facts 写核变化。"""

        raise StaffDutyBindingLegacyWriterDisabled(
            "旧任职—责任组连带写入口已机械禁用；任职必须经正式 Canonical 通道写入。"
        )

        # The former implementation remains below only as the explicitly recorded
        # rebuild debt. This public entry point must not reach it before that debt
        # is replaced by an approved Canonical assignment composition.

        actor = context.actor
        if type(actor) is not StaffActorContext:
            raise StaffDutyBindingContextRequired("该动作必须明确选择当前教职工任职。")
        if command.person_id == actor.person_id:
            raise StaffDutyBindingDutySeparationRequired("正式任职受益人不得自批自授。")
        identity = self._idempotency_identity(command, context, actor)
        identifiers = self._derive_ids(context, actor)
        try:
            return self._execute(command, context, actor, identity, identifiers)
        except CommitOutcomeUnknown:
            return self._probe(command, context, identity, identifiers)
        except DBAPIError as exc:
            self._raise_database_error(exc)

    def _execute(
        self,
        command: ConfirmStaffDutyBindingCommand,
        context: CommandExecutionContext,
        actor: StaffActorContext,
        identity: CommandIdempotencyIdentity,
        identifiers: _BindingIds,
    ) -> ConfirmStaffDutyBindingOutcome:
        raise StaffDutyBindingLegacyWriterDisabled(
            "旧任职—责任组连带写入口已机械禁用；任职必须经正式 Canonical 通道写入。"
        )

        with self._uow_factory.open_command(context) as uow:
            record = uow.idempotency.acquire(identity)
            if record.status == "succeeded":
                return self._verified_outcome(
                    uow,
                    command,
                    actor,
                    record,
                    identifiers,
                    kind="replayed",
                )
            if record.status != "in_progress":
                raise StaffDutyBindingOutcomeVerificationFailed("该确认命令已有不可重放的终态。")

            database_now = uow.current_database_time()
            locked_actor = uow.assignments.lock_assignment(
                actor.school_id,
                actor.assignment_id,
            )
            if (
                locked_actor is None
                or locked_actor.person_id != actor.person_id
                or locked_actor.version != actor.assignment_version
                or locked_actor.status != "confirmed"
                or not self._contains(locked_actor.interval, database_now)
            ):
                raise StaffDutyBindingContextRequired("当前教职工任职已经失效或发生变化。")

            # 固定全局顺序：人员 → 组织 → 责任组；同类目标扩展时必须按 UUID 升序。
            person = uow.people.lock_person(
                actor.school_id,
                command.person_id,
                as_of=database_now,
            )
            unit = uow.assignments.lock_organization_unit(
                actor.school_id,
                command.organization_unit_id,
                as_of=database_now,
            )
            group = uow.responsibility_memberships.lock_group(
                actor.school_id,
                command.responsibility_group_id,
            )
            if person is None or unit is None or group is None:
                raise StaffDutyBindingTargetNotFound("确认卡目标不存在于当前学校。")
            if person.status != "active" or unit.status != "active" or group.status != "active":
                raise StaffDutyBindingTargetNotFound("确认卡目标已经停用，不能建立新关系。")
            if (
                person.version != command.expected_person_version
                or unit.version != command.expected_organization_unit_version
                or group.version != command.expected_group_version
            ):
                raise StaffDutyBindingVersionConflict("确认卡版本已经变化，请重新预览。")

            authorization_facts = StaffDutyBindingAuthorizationFacts(
                school_id=actor.school_id,
                actor_assignment_id=actor.assignment_id,
                actor_semantic_role_code=locked_actor.semantic_role_code,
                target_person_id=command.person_id,
                target_organization_unit_id=command.organization_unit_id,
                responsibility_group_id=command.responsibility_group_id,
            )
            if not self._authorizer.is_allowed(authorization_facts):
                raise StaffDutyBindingPermissionDenied("当前任职没有确认该变化的服务端权限。")

            assignment = self._new_assignment(command, actor.school_id, identifiers)
            membership = self._new_membership(command, actor.school_id, identifiers)
            try:
                uow.assignments.add_confirmed_assignment(assignment)
            except DBAPIError as exc:
                raise StaffDutyBindingAssignmentWriteFailed("正式任职写入失败。") from exc
            try:
                uow.responsibility_memberships.add_confirmed_membership(membership)
            except DBAPIError as exc:
                raise StaffDutyBindingMembershipWriteFailed("责任组成员写入失败。") from exc

            audit_record = self._audit_record(command, identifiers)
            try:
                uow.audit.append(audit_record)
            except DBAPIError as exc:
                raise StaffDutyBindingAuditWriteFailed("业务审计写入失败。") from exc

            outbox_event = OutboxEvent(
                event_id=identifiers.outbox_event_id,
                school_id=actor.school_id,
                event_key=f"master-data.staff-duty-binding:{identifiers.assignment_id}",
                event_type="master_data.assignment.changed",
                aggregate_type="staff_assignment",
                aggregate_id=identifiers.assignment_id,
                aggregate_version=1,
                occurred_at=database_now,
            )
            try:
                uow.outbox.append(outbox_event)
            except DBAPIError as exc:
                raise StaffDutyBindingOutboxWriteFailed("Outbox 写入失败。") from exc

            result = SafeCommandResult(
                resource_type=_ASSIGNMENT_RESOURCE_TYPE,
                resource_id=identifiers.assignment_id,
                resource_version=1,
                outcome_code="created",
            )
            try:
                uow.idempotency.mark_succeeded(
                    identity,
                    result_type=_RESULT_TYPE,
                    result=result,
                )
            except DBAPIError as exc:
                raise StaffDutyBindingIdempotencyWriteFailed("幂等终态写入失败。") from exc
            uow.commit()
            return self._outcome(identifiers, kind="created")

    def _probe(
        self,
        command: ConfirmStaffDutyBindingCommand,
        context: CommandExecutionContext,
        identity: CommandIdempotencyIdentity,
        identifiers: _BindingIds,
    ) -> ConfirmStaffDutyBindingOutcome:
        actor = context.actor
        if type(actor) is not StaffActorContext:
            raise StaffDutyBindingContextRequired("结果探测必须绑定原教职工任职。")
        with self._uow_factory.open_probe(context) as uow:
            record = uow.idempotency.probe(identity)
            if record is None or record.status != "succeeded":
                raise StaffDutyBindingOutcomeVerificationFailed(
                    "提交结果未知且未找到已成功的固定幂等终态。"
                )
            return self._verified_outcome(
                uow,
                command,
                actor,
                record,
                identifiers,
                kind="replayed",
            )

    def _verified_outcome(
        self,
        uow: StaffDutyBindingOutcomeProbe,
        command: ConfirmStaffDutyBindingCommand,
        actor: StaffActorContext,
        record: IdempotencyRecordProjection,
        identifiers: _BindingIds,
        *,
        kind: Literal["created", "replayed"],
    ) -> ConfirmStaffDutyBindingOutcome:
        result = record.result
        if (
            record.result_type != _RESULT_TYPE
            or result is None
            or result.resource_type != _ASSIGNMENT_RESOURCE_TYPE
            or result.resource_id != identifiers.assignment_id
            or result.resource_version != 1
            or result.outcome_code != "created"
        ):
            raise StaffDutyBindingOutcomeVerificationFailed("幂等结果结构与确认命令不一致。")
        assignment = self._new_assignment(command, actor.school_id, identifiers)
        membership = self._new_membership(command, actor.school_id, identifiers)
        if not uow.assignments.confirmed_assignment_matches(assignment):
            raise StaffDutyBindingOutcomeVerificationFailed("固定正式任职行缺失或字段不一致。")
        if not uow.responsibility_memberships.confirmed_membership_matches(membership):
            raise StaffDutyBindingOutcomeVerificationFailed("固定责任组成员行缺失或字段不一致。")
        if not uow.outbox.contains_staff_assignment_event(
            event_id=identifiers.outbox_event_id,
            school_id=actor.school_id,
            assignment_id=identifiers.assignment_id,
            assignment_version=1,
        ):
            raise StaffDutyBindingOutcomeVerificationFailed("固定 Outbox 事件缺失或字段不一致。")
        return self._outcome(identifiers, kind=kind)

    @staticmethod
    def _new_assignment(
        command: ConfirmStaffDutyBindingCommand,
        school_id: SchoolId,
        identifiers: _BindingIds,
    ) -> NewConfirmedStaffAssignment:
        return NewConfirmedStaffAssignment(
            assignment_id=identifiers.assignment_id,
            school_id=school_id,
            person_id=command.person_id,
            organization_unit_id=command.organization_unit_id,
            position_title=command.position_title,
            semantic_role_code=command.semantic_role_code,
            interval=command.interval,
            approval_reference=command.confirmation_reason,
        )

    @staticmethod
    def _new_membership(
        command: ConfirmStaffDutyBindingCommand,
        school_id: SchoolId,
        identifiers: _BindingIds,
    ) -> NewConfirmedResponsibilityMembership:
        return NewConfirmedResponsibilityMembership(
            membership_id=identifiers.membership_id,
            school_id=school_id,
            responsibility_group_id=command.responsibility_group_id,
            assignment_id=identifiers.assignment_id,
            interval=command.interval,
        )

    @staticmethod
    def _audit_record(
        command: ConfirmStaffDutyBindingCommand,
        identifiers: _BindingIds,
    ) -> BusinessAuditRecord:
        structural_targets = (
            command.person_id,
            command.organization_unit_id,
            command.responsibility_group_id,
        )
        return BusinessAuditRecord(
            event_type="master_data.assignment.changed",
            target_type="staff_assignment",
            target_id=identifiers.assignment_id,
            before_summary=BusinessAuditSummary(
                state_code="assignment.absent",
                stable_reference_ids=structural_targets,
            ),
            after_summary=BusinessAuditSummary(
                state_code="assignment.confirmed",
                resource_version=1,
                stable_reference_ids=(
                    *structural_targets,
                    identifiers.assignment_id,
                    identifiers.membership_id,
                ),
                fact_codes=("assignment.updated",),
            ),
            reason_code="manual.assignment-confirmed",
        )

    @staticmethod
    def _idempotency_identity(
        command: ConfirmStaffDutyBindingCommand,
        context: CommandExecutionContext,
        actor: StaffActorContext,
    ) -> CommandIdempotencyIdentity:
        request_json = json.dumps(
            {
                "schema": "confirm_staff_duty_binding.v1",
                "command": command.model_dump(mode="json"),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return CommandIdempotencyIdentity(
            school_id=actor.school_id,
            action_code=_ACTION_CODE,
            actor_user_id=actor.account_user_id,
            actor_person_id=actor.person_id,
            actor_context_type="assignment",
            actor_assignment_id=actor.assignment_id,
            actor_assignment_version=actor.assignment_version,
            resource_type=_IDEMPOTENCY_RESOURCE_TYPE,
            resource_id=str(actor.assignment_id),
            idempotency_key_hash=sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
            request_sha256=sha256(request_json.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _derive_ids(
        context: CommandExecutionContext,
        actor: StaffActorContext,
    ) -> _BindingIds:
        key_hash = sha256(context.idempotency_key.encode("utf-8")).hexdigest()
        operation_id = uuid5(
            _ID_NAMESPACE,
            "\x1f".join(
                (
                    str(actor.school_id),
                    str(actor.assignment_id),
                    str(actor.assignment_version),
                    _ACTION_CODE,
                    key_hash,
                )
            ),
        )
        return _BindingIds(
            assignment_id=AssignmentId(uuid5(operation_id, "staff-assignment")),
            membership_id=uuid5(operation_id, "responsibility-membership"),
            outbox_event_id=uuid5(operation_id, "outbox-event"),
        )

    @staticmethod
    def _contains(interval: EffectiveInterval, as_of: datetime) -> bool:
        return interval.valid_from <= as_of and (
            interval.valid_to is None or interval.valid_to > as_of
        )

    @staticmethod
    def _outcome(
        identifiers: _BindingIds,
        *,
        kind: Literal["created", "replayed"],
    ) -> ConfirmStaffDutyBindingOutcome:
        return ConfirmStaffDutyBindingOutcome(
            kind=kind,
            assignment_id=identifiers.assignment_id,
            membership_id=identifiers.membership_id,
            outbox_event_id=identifiers.outbox_event_id,
            assignment_version=1,
            membership_version=1,
        )

    @staticmethod
    def _raise_database_error(error: DBAPIError) -> NoReturn:
        sqlstate = getattr(error.orig, "sqlstate", None)
        if sqlstate == "55P03":
            raise StaffDutyBindingRetryableConflict(
                "数据库锁等待超时，请重新预览后由人工重试。"
            ) from error
        if sqlstate == "28000":
            raise StaffDutyBindingContextRequired("当前教职工任职已经失效。") from error
        raise error
