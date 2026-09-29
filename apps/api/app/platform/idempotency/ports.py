from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.platform.contracts import SchoolId, SemanticCode
from app.platform.idempotency.contracts import (
    CommandIdempotencyIdentity,
    IdempotencyAcquisition,
    IdempotencyRecordOwnerProjection,
    IdempotencyRecordProjection,
    SafeCommandResult,
    Sha256Hex,
)


class CommandIdempotencyProbeReader(Protocol):
    """只按固定命令身份读取幂等终态，不获取锁或写入。"""

    def probe(
        self,
        identity: CommandIdempotencyIdentity,
    ) -> IdempotencyRecordProjection | None: ...

    def find_by_record_id(
        self,
        school_id: SchoolId,
        record_id: UUID,
    ) -> IdempotencyRecordOwnerProjection | None:
        """按学校和记录标识读取窄归属投影，不获取行锁。"""

        raise NotImplementedError("Protocol declaration only.")


class CommandIdempotencyRepository(CommandIdempotencyProbeReader, Protocol):
    """定义正式命令在调用方事务内使用的幂等记录边界。"""

    def acquire(self, identity: CommandIdempotencyIdentity) -> IdempotencyRecordProjection: ...

    def acquire_with_origin(
        self,
        identity: CommandIdempotencyIdentity,
    ) -> IdempotencyAcquisition: ...

    def mark_succeeded(
        self,
        identity: CommandIdempotencyIdentity,
        *,
        result_type: SemanticCode,
        result: SafeCommandResult,
    ) -> IdempotencyRecordProjection: ...

    def mark_terminal_failure(
        self,
        identity: CommandIdempotencyIdentity,
        *,
        result_type: SemanticCode,
        result: SafeCommandResult,
    ) -> IdempotencyRecordProjection: ...

    def lock_by_record_id(
        self,
        school_id: SchoolId,
        record_id: UUID,
    ) -> IdempotencyRecordOwnerProjection | None:
        """按学校和记录标识锁定旧命令并返回窄归属投影。"""

        raise NotImplementedError("Protocol declaration only.")

    def mark_record_terminal_failure(
        self,
        school_id: SchoolId,
        record_id: UUID,
        *,
        expected_request_sha256: Sha256Hex,
        result_type: SemanticCode,
        result: SafeCommandResult,
    ) -> IdempotencyRecordProjection:
        """只以记录标识、学校、旧请求摘要和 in_progress 状态执行终态 CAS。"""

        raise NotImplementedError("Protocol declaration only.")
