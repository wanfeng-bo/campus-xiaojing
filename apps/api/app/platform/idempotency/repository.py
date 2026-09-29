from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.platform.contracts import AssignmentId, PersonId, SchoolId, SemanticCode
from app.platform.idempotency.contracts import (
    CommandIdempotencyIdentity,
    IdempotencyAcquisition,
    IdempotencyPayloadConflict,
    IdempotencyRecordOwnerProjection,
    IdempotencyRecordProjection,
    SafeCommandResult,
    Sha256Hex,
)
from app.platform.idempotency.models import CommandIdempotencyRecord


class SqlAlchemyCommandIdempotencyProbeReader:
    """只读事务中的固定幂等身份读取器。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def probe(
        self,
        identity: CommandIdempotencyIdentity,
    ) -> IdempotencyRecordProjection | None:
        """在新的只读事务中读取终态，不获取行锁或尝试写入。"""

        record = self._session.scalar(self._identity_statement(identity))
        if record is None:
            return None
        if record.request_sha256 != identity.request_sha256:
            raise IdempotencyPayloadConflict("同一幂等键不能用于不同的规范请求。")
        return self._projection(record)

    def find_by_record_id(
        self,
        school_id: SchoolId,
        record_id: UUID,
    ) -> IdempotencyRecordOwnerProjection | None:
        """按同校稳定记录标识读取归属字段，不获取行锁。"""

        record = self._session.scalar(
            select(CommandIdempotencyRecord).where(
                CommandIdempotencyRecord.school_id == school_id,
                CommandIdempotencyRecord.id == record_id,
            )
        )
        return self._owner_projection(record) if record is not None else None

    @staticmethod
    def _identity_statement(identity: CommandIdempotencyIdentity):
        return select(CommandIdempotencyRecord).where(
            CommandIdempotencyRecord.school_id == identity.school_id,
            CommandIdempotencyRecord.action_code == identity.action_code,
            CommandIdempotencyRecord.actor_context_key == identity.actor_context_key,
            CommandIdempotencyRecord.resource_type == identity.resource_type,
            CommandIdempotencyRecord.resource_id == identity.resource_id,
            CommandIdempotencyRecord.idempotency_key_hash == identity.idempotency_key_hash,
        )

    @staticmethod
    def _projection(record: CommandIdempotencyRecord) -> IdempotencyRecordProjection:
        result = (
            SafeCommandResult.model_validate_json(json.dumps(record.result_payload))
            if record.result_payload is not None
            else None
        )
        return IdempotencyRecordProjection(
            record_id=record.id,
            status=record.status,  # type: ignore[arg-type]
            request_sha256=record.request_sha256,
            result_type=record.result_type,
            result=result,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @classmethod
    def _owner_projection(
        cls,
        record: CommandIdempotencyRecord,
    ) -> IdempotencyRecordOwnerProjection:
        projection = cls._projection(record)
        return IdempotencyRecordOwnerProjection(
            **projection.model_dump(),
            action_code=record.action_code,
            actor_user_id=record.actor_user_id,
            actor_person_id=PersonId(record.actor_person_id),
            actor_context_type=record.actor_context_type,  # type: ignore[arg-type]
            actor_assignment_id=(
                AssignmentId(record.actor_assignment_id)
                if record.actor_assignment_id is not None
                else None
            ),
            actor_assignment_version=record.actor_assignment_version,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            idempotency_key_hash=record.idempotency_key_hash,
        )


class SqlAlchemyCommandIdempotencyRepository(SqlAlchemyCommandIdempotencyProbeReader):
    """使用唯一约束与行锁在调用方事务内收敛重复命令。"""

    def acquire(self, identity: CommandIdempotencyIdentity) -> IdempotencyRecordProjection:
        return self.acquire_with_origin(identity).record

    def lock_by_record_id(
        self,
        school_id: SchoolId,
        record_id: UUID,
    ) -> IdempotencyRecordOwnerProjection | None:
        """锁定同校旧命令记录，并仅返回稳定归属字段。"""

        record = self._session.scalar(
            select(CommandIdempotencyRecord)
            .where(
                CommandIdempotencyRecord.school_id == school_id,
                CommandIdempotencyRecord.id == record_id,
            )
            .with_for_update()
        )
        return self._owner_projection(record) if record is not None else None

    def acquire_with_origin(
        self,
        identity: CommandIdempotencyIdentity,
    ) -> IdempotencyAcquisition:
        """原子获取记录，并返回它是否确由本事务新建。"""

        values = {
            "id": uuid4(),
            **identity.model_dump(),
            "actor_context_key": identity.actor_context_key,
        }
        inserted_id = self._session.scalar(
            insert(CommandIdempotencyRecord)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_command_idempotency_records_scope_key")
            .returning(CommandIdempotencyRecord.id)
        )
        record = self._session.scalar(self._identity_statement(identity).with_for_update())
        if record is None:
            raise RuntimeError("幂等记录在唯一约束命中后无法读取。")
        if record.request_sha256 != identity.request_sha256:
            raise IdempotencyPayloadConflict("同一幂等键不能用于不同的规范请求。")
        return IdempotencyAcquisition(
            record=self._projection(record),
            created=inserted_id is not None,
        )

    def mark_succeeded(
        self,
        identity: CommandIdempotencyIdentity,
        *,
        result_type: SemanticCode,
        result: SafeCommandResult,
    ) -> IdempotencyRecordProjection:
        return self._mark_terminal(identity, "succeeded", result_type, result)

    def mark_terminal_failure(
        self,
        identity: CommandIdempotencyIdentity,
        *,
        result_type: SemanticCode,
        result: SafeCommandResult,
    ) -> IdempotencyRecordProjection:
        return self._mark_terminal(identity, "terminal_failure", result_type, result)

    def mark_record_terminal_failure(
        self,
        school_id: SchoolId,
        record_id: UUID,
        *,
        expected_request_sha256: Sha256Hex,
        result_type: SemanticCode,
        result: SafeCommandResult,
    ) -> IdempotencyRecordProjection:
        """用单条条件 UPDATE 终止旧命令；仅相同既有终态允许幂等返回。"""

        update_result = cast(
            CursorResult[Any],
            self._session.execute(
                update(CommandIdempotencyRecord)
                .where(
                    CommandIdempotencyRecord.id == record_id,
                    CommandIdempotencyRecord.school_id == school_id,
                    CommandIdempotencyRecord.status == "in_progress",
                    CommandIdempotencyRecord.request_sha256 == expected_request_sha256,
                )
                .values(
                    status="terminal_failure",
                    result_type=result_type,
                    result_payload=result.model_dump(mode="json"),
                    updated_at=func.now(),
                )
            ),
        )
        self._session.flush()
        record = self._session.scalar(
            select(CommandIdempotencyRecord).where(
                CommandIdempotencyRecord.id == record_id,
                CommandIdempotencyRecord.school_id == school_id,
            )
        )
        if update_result.rowcount == 1:
            if record is None:
                raise RuntimeError("旧幂等记录完成终态 CAS 后无法读取。")
            return self._projection(record)

        existing_result = self._projection(record) if record is not None else None
        if (
            existing_result is not None
            and existing_result.request_sha256 == expected_request_sha256
            and existing_result.status == "terminal_failure"
            and existing_result.result_type == result_type
            and existing_result.result == result
        ):
            return existing_result
        raise IdempotencyPayloadConflict("旧幂等记录已不存在或不再匹配预期终态。")

    def _mark_terminal(
        self,
        identity: CommandIdempotencyIdentity,
        status: str,
        result_type: SemanticCode,
        result: SafeCommandResult,
    ) -> IdempotencyRecordProjection:
        record = self._session.scalar(self._identity_statement(identity).with_for_update())
        if record is None:
            raise LookupError("幂等记录不存在。")
        if record.request_sha256 != identity.request_sha256:
            raise IdempotencyPayloadConflict("同一幂等键不能用于不同的规范请求。")
        if record.status != "in_progress":
            return self._projection(record)
        update_result = cast(
            CursorResult[Any],
            self._session.execute(
                update(CommandIdempotencyRecord)
                .where(
                    CommandIdempotencyRecord.id == record.id,
                    CommandIdempotencyRecord.status == "in_progress",
                )
                .values(
                    status=status,
                    result_type=result_type,
                    result_payload=result.model_dump(mode="json"),
                    updated_at=func.now(),
                )
            ),
        )
        if update_result.rowcount != 1:
            raise RuntimeError("幂等终态未能从 in_progress 原子转换。")
        self._session.flush()
        self._session.expire(record)
        return self._projection(record)
