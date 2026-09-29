from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Protocol
from uuid import UUID

from app.platform.contracts import AssignmentId, PersonId, SchoolId
from app.platform.idempotency.contracts import (
    IdempotencyRecordOwnerProjection,
    Sha256Hex,
)
from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from app.platform.master_data_imports.models import (
    MasterDataImportBatch,
    MasterDataImportCommandOutcome,
    MasterDataImportTakeover,
    MasterDataImportUnit,
)
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1, PreflightReportV1
from app.platform.school_facts.ports import SchoolFactChangeProvenance


class PrivateImportSourceExpired(KeyError):
    """受保护来源已过期并被清除。"""


class PrivateImportSourceAccessDenied(KeyError):
    """受保护来源不属于当前服务端身份和会话。"""


class PrivateImportSourceCapacityExceeded(RuntimeError):
    """受保护来源的有界容量已用尽。"""


@dataclass(frozen=True, slots=True)
class PrivateSourceOwner:
    school_id: str
    actor_user_id: int
    session_sha256: str

    def __post_init__(self) -> None:
        try:
            UUID(self.school_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("私有来源学校标识无效。") from None
        if type(self.actor_user_id) is not int or self.actor_user_id <= 0:
            raise ValueError("私有来源操作人标识无效。")
        if len(self.session_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.session_sha256
        ):
            raise ValueError("私有来源会话摘要无效。")


class PrivateImportSourceStore(Protocol):
    def put(
        self,
        *,
        filename_summary: str,
        content: bytes,
        owner: PrivateSourceOwner,
    ) -> ProtectedImportSourceRef: ...

    def open(
        self,
        opaque_ref: str,
        *,
        owner: PrivateSourceOwner,
        now: datetime | None = None,
    ) -> BinaryIO: ...

    def describe(
        self,
        opaque_ref: str,
        *,
        owner: PrivateSourceOwner,
        now: datetime | None = None,
    ) -> ProtectedImportSourceRef: ...

    def delete(self, opaque_ref: str, *, owner: PrivateSourceOwner) -> None: ...


@dataclass(frozen=True, slots=True)
class StoredSchoolFactPlan:
    plan_ref: str
    owner: PrivateSourceOwner
    change_set: CanonicalFactChangeSetV1
    provenance: SchoolFactChangeProvenance
    preflight: PreflightReportV1
    source_refs: tuple[str, ...]
    expires_at: datetime
    package_request_json: str | None = None


@dataclass(frozen=True, slots=True)
class SchoolFactPlanLocator:
    plan_ref: str
    owner: PrivateSourceOwner
    source_sha256: str
    plan_sha256: str
    expires_at: datetime
    is_single_school_reactivation: bool = False


class SchoolFactPlanStore(Protocol):
    def put(
        self,
        *,
        owner: PrivateSourceOwner,
        change_set: CanonicalFactChangeSetV1,
        provenance: SchoolFactChangeProvenance,
        preflight: PreflightReportV1,
        source_refs: tuple[str, ...] = (),
        package_request_json: str | None = None,
    ) -> StoredSchoolFactPlan: ...

    def get(self, plan_ref: str, *, owner: PrivateSourceOwner) -> StoredSchoolFactPlan: ...

    def locate(self, plan_ref: str, *, owner: PrivateSourceOwner) -> SchoolFactPlanLocator: ...

    def retire(self, plan_ref: str, *, owner: PrivateSourceOwner) -> None: ...

    def delete(self, plan_ref: str, *, owner: PrivateSourceOwner) -> None: ...


@dataclass(frozen=True, slots=True)
class LockedTakeoverAssignment:
    """专用接管边界返回的最小原任职投影。"""

    assignment_id: AssignmentId
    person_id: PersonId
    version: int
    status: str
    valid_from: datetime
    valid_to: datetime | None


@dataclass(frozen=True, slots=True)
class ImportUnitOutcomeSnapshot:
    """数据库在首次命令结果事务内冻结的逐单元终态事实。"""

    sequence: int
    status: str
    version: int
    target_id: UUID | None
    target_version: int | None
    error_code: str | None
    error_field_code: str | None


@dataclass(frozen=True, slots=True)
class SucceededImportAttemptOutcome:
    """经窄数据库边界交叉证明的历史 succeeded 命令与首次结果。"""

    idempotency_record_id: UUID
    batch_id: UUID
    action_code: str
    status: str
    batch_version: int
    succeeded_units: tuple[int, ...]
    failed_units: tuple[int, ...]
    executed_units: tuple[int, ...]
    unit_outcome_snapshots: tuple[ImportUnitOutcomeSnapshot, ...]


class MasterDataImportOutcomeReader(Protocol):
    """只按学校和固定幂等记录读取不可变导入结果。"""

    def find_outcome(
        self,
        school_id: SchoolId,
        idempotency_record_id: UUID,
    ) -> MasterDataImportCommandOutcome | None:
        """返回同校固定幂等记录的首次导入结果；不存在时返回空。"""

        ...


class MasterDataImportRecoveryReader(Protocol):
    """读取接管提交后的稳定恢复事实，不获取行锁或改变状态。"""

    def find_batch(
        self,
        school_id: SchoolId,
        batch_id: UUID,
    ) -> MasterDataImportBatch | None:
        """按学校和批次标识读取恢复批次；不存在时返回空。"""

        raise NotImplementedError("Protocol declaration only.")

    def find_units(
        self,
        school_id: SchoolId,
        batch_id: UUID,
    ) -> tuple[MasterDataImportUnit, ...]:
        """按 sequence 稳定顺序读取同校批次的全部单元。"""

        raise NotImplementedError("Protocol declaration only.")

    def find_takeover_by_new_record(
        self,
        school_id: SchoolId,
        new_record_id: UUID,
    ) -> MasterDataImportTakeover | None:
        """按学校和新幂等记录读取唯一接管事实。"""

        raise NotImplementedError("Protocol declaration only.")

    def find_attempt_lineage(
        self,
        school_id: SchoolId,
        new_record_id: UUID,
    ) -> tuple[UUID, ...]:
        """递归返回全部 previous 记录，不含传入的新记录。

        结果按对应接管记录的 ``created_at ASC, id ASC`` 从最早到最近稳定
        排序并去重；调用方如需完整链，必须显式在末尾追加 ``new_record_id``。
        """

        raise NotImplementedError("Protocol declaration only.")

    def find_succeeded_attempt_outcome(
        self,
        school_id: SchoolId,
        batch_id: UUID,
        idempotency_record_id: UUID,
    ) -> SucceededImportAttemptOutcome | None:
        """原子读取已交叉证明的同批次 succeeded 幂等终态及其首次结果。"""

        raise NotImplementedError("Protocol declaration only.")


class MasterDataImportRepository(
    MasterDataImportOutcomeReader,
    MasterDataImportRecoveryReader,
    Protocol,
):
    """导入编排器所需的窄持久化边界；不拥有提交或 Session 生命周期。"""

    def add_batch(self, batch: MasterDataImportBatch) -> None:
        """加入新导入批次并 flush，但不提交事务。"""

        ...

    def add_unit(self, unit: MasterDataImportUnit) -> None:
        """加入新导入原子单元并 flush，但不提交事务。"""

        ...

    def add_outcome(self, outcome: MasterDataImportCommandOutcome) -> None:
        """加入不可变命令结果并 flush，但不提交事务。"""

        ...

    def add_takeover(self, takeover: MasterDataImportTakeover) -> None:
        """追加不可变接管事实并 flush，但不提交事务。"""

        raise NotImplementedError("Protocol declaration only.")

    def find_active_idempotency_record_id(
        self,
        school_id: SchoolId,
        batch_id: UUID,
    ) -> UUID | None:
        """无锁预读同校批次的活动命令标识，仅供固定锁序定位旧记录。"""

        raise NotImplementedError("Protocol declaration only.")

    def lock_takeover_source(
        self,
        school_id: SchoolId,
        batch_id: UUID,
        record_id: UUID,
    ) -> IdempotencyRecordOwnerProjection | None:
        """经专用 definer 仅锁定批次当前活动的旧命令，并返回安全归属投影。"""

        raise NotImplementedError("Protocol declaration only.")

    def lock_takeover_assignment(
        self,
        school_id: SchoolId,
        batch_id: UUID,
        record_id: UUID,
        assignment_id: AssignmentId,
    ) -> LockedTakeoverAssignment | None:
        """经专用 definer 仅锁定旧命令引用的原任职；缺失时返回空。"""

        raise NotImplementedError("Protocol declaration only.")

    def supersede_takeover_source(
        self,
        school_id: SchoolId,
        batch_id: UUID,
        record_id: UUID,
        *,
        expected_request_sha256: Sha256Hex,
        batch_version_before: int,
    ) -> None:
        """终验同事务接管 bundle，以固定结果 CAS 旧命令并追加固定审计。"""

        raise NotImplementedError("Protocol declaration only.")

    def lock_batch(
        self,
        school_id: SchoolId,
        batch_id: UUID,
    ) -> MasterDataImportBatch | None:
        """按学校锁定一个批次；不存在时返回空。"""

        ...

    def lock_batch_by_source(
        self,
        school_id: SchoolId,
        *,
        source_kind: str,
        source_ref: str,
        source_sha256: str,
        mode: str,
    ) -> MasterDataImportBatch | None:
        """锁定同一受保护来源的既有批次，防止重复登记。"""

        ...

    def lock_units(
        self,
        school_id: SchoolId,
        batch_id: UUID,
    ) -> tuple[MasterDataImportUnit, ...]:
        """按固定序号顺序锁定批次的全部原子单元。"""

        ...

    def lock_unit(
        self,
        school_id: SchoolId,
        batch_id: UUID,
        sequence: int,
    ) -> MasterDataImportUnit | None:
        """锁定批次中的指定原子单元；不存在时返回空。"""

        ...

    def lock_takeover_by_previous_record(
        self,
        school_id: SchoolId,
        previous_record_id: UUID,
    ) -> MasterDataImportTakeover | None:
        """锁事务级 previous-record advisory 唯一键，并重验既有接管事实。"""

        raise NotImplementedError("Protocol declaration only.")

    def save_batch(self, batch: MasterDataImportBatch) -> None:
        """flush 已由应用层校验的批次变化，但不提交事务。"""

        ...

    def save_unit(self, unit: MasterDataImportUnit) -> None:
        """flush 已由应用层校验的单元变化，但不提交事务。"""

        ...

    def external_reference_exists(
        self,
        school_id: SchoolId,
        external_reference: str,
    ) -> bool:
        """判断当前学校是否已有已确认的同一稳定外部编号。"""

        ...

    def same_display_name_exists(
        self,
        school_id: SchoolId,
        display_name: str,
    ) -> bool:
        """判断当前学校是否存在同名人员，用于生成非阻断提示。"""

        ...

    def lock_display_name_candidate(
        self,
        school_id: SchoolId,
        display_name: str,
    ) -> None:
        """锁定当前学校的同名候选键，使检查与人员写入保持串行。"""

        ...


class MasterDataImportAuditWriter(Protocol):
    """只允许导入 Handler 追加三类固定安全审计。"""

    def append_person_imported(
        self,
        *,
        batch_id: UUID,
        unit_id: UUID,
        person_id: UUID,
        source_kind: str,
        continued_same_name: bool,
    ) -> None:
        """在当前事务追加人员导入安全审计。"""

        ...

    def append_calendar_published(
        self,
        *,
        batch_id: UUID,
        unit_id: UUID,
        calendar_version_id: UUID,
    ) -> None:
        """在当前事务追加校历发布安全审计。"""

        ...

    def append_ordinary_import_taken_over(
        self,
        *,
        batch_id: UUID,
        takeover_id: UUID,
    ) -> None:
        """在当前事务追加不含自由文本的普通导入接管审计。"""

        ...
