from __future__ import annotations

from uuid import UUID

from sqlalchemy import exists, func, select, text
from sqlalchemy.orm import Session

from app.platform.contracts import AssignmentId, PersonId, SchoolId
from app.platform.directory.models import SchoolPersonName
from app.platform.idempotency.contracts import (
    IdempotencyPayloadConflict,
    IdempotencyRecordOwnerProjection,
    Sha256Hex,
)
from app.platform.master_data_imports.models import (
    MasterDataImportBatch,
    MasterDataImportCommandOutcome,
    MasterDataImportTakeover,
    MasterDataImportUnit,
)
from app.platform.master_data_imports.ports import (
    ImportUnitOutcomeSnapshot,
    LockedTakeoverAssignment,
    SucceededImportAttemptOutcome,
)


class SqlAlchemyMasterDataImportOutcomeReader:
    """只读固定幂等记录对应的不可变导入结果。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_outcome(
        self,
        school_id: SchoolId,
        idempotency_record_id: UUID,
    ) -> MasterDataImportCommandOutcome | None:
        """按学校和幂等记录读取首次结果，不获取锁。"""

        return self._session.scalar(
            select(MasterDataImportCommandOutcome).where(
                MasterDataImportCommandOutcome.school_id == school_id,
                MasterDataImportCommandOutcome.idempotency_record_id == idempotency_record_id,
            )
        )


class SqlAlchemyMasterDataImportRecoveryReader(SqlAlchemyMasterDataImportOutcomeReader):
    """读取接管提交未知后用于交叉核对的同校稳定事实。"""

    def find_batch(
        self,
        school_id: SchoolId,
        batch_id: UUID,
    ) -> MasterDataImportBatch | None:
        """按学校和批次标识读取批次，不获取行锁。"""

        return self._session.scalar(
            select(MasterDataImportBatch).where(
                MasterDataImportBatch.school_id == school_id,
                MasterDataImportBatch.id == batch_id,
            )
        )

    def find_units(
        self,
        school_id: SchoolId,
        batch_id: UUID,
    ) -> tuple[MasterDataImportUnit, ...]:
        """按 sequence 稳定顺序读取同校批次的全部单元。"""

        return tuple(
            self._session.scalars(
                select(MasterDataImportUnit)
                .where(
                    MasterDataImportUnit.school_id == school_id,
                    MasterDataImportUnit.batch_id == batch_id,
                )
                .order_by(MasterDataImportUnit.sequence)
            ).all()
        )

    def find_takeover_by_new_record(
        self,
        school_id: SchoolId,
        new_record_id: UUID,
    ) -> MasterDataImportTakeover | None:
        """按学校和新幂等记录读取唯一接管事实，不获取行锁。"""

        return self._session.scalar(
            select(MasterDataImportTakeover).where(
                MasterDataImportTakeover.school_id == school_id,
                MasterDataImportTakeover.new_idempotency_record_id == new_record_id,
            )
        )

    def find_attempt_lineage(
        self,
        school_id: SchoolId,
        new_record_id: UUID,
    ) -> tuple[UUID, ...]:
        """经固定只读边界返回已完整验证的 root→previous 接管链。"""

        rows = self._session.execute(
            text(
                "SELECT record_id FROM app.read_master_data_import_attempt_lineage("
                ":school_id, :new_record_id)"
            ),
            {"school_id": school_id, "new_record_id": new_record_id},
        ).all()
        return tuple(record_id for (record_id,) in rows)

    def find_succeeded_attempt_outcome(
        self,
        school_id: SchoolId,
        batch_id: UUID,
        idempotency_record_id: UUID,
    ) -> SucceededImportAttemptOutcome | None:
        """经固定 definer 边界原子证明历史 succeeded 命令及其首次结果。"""

        row = (
            self._session.execute(
                text(
                    "SELECT * FROM app.read_master_data_import_succeeded_attempt("
                    ":school_id, :batch_id, :idempotency_record_id)"
                ),
                {
                    "school_id": school_id,
                    "batch_id": batch_id,
                    "idempotency_record_id": idempotency_record_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        unit_outcome_snapshots = self._parse_unit_outcome_snapshots(row["unit_outcome_snapshots"])
        if unit_outcome_snapshots is None:
            return None
        return SucceededImportAttemptOutcome(
            idempotency_record_id=row["idempotency_record_id"],
            batch_id=row["batch_id"],
            action_code=row["action_code"],
            status=row["status"],
            batch_version=row["batch_version"],
            succeeded_units=tuple(row["succeeded_units"]),
            failed_units=tuple(row["failed_units"]),
            executed_units=tuple(row["executed_units"]),
            unit_outcome_snapshots=unit_outcome_snapshots,
        )

    @staticmethod
    def _parse_unit_outcome_snapshots(
        value: object,
    ) -> tuple[ImportUnitOutcomeSnapshot, ...] | None:
        """只接受数据库固定 JSON 形态；旧行或结构漂移均失败关闭。"""

        if not isinstance(value, list):
            return None
        snapshots: list[ImportUnitOutcomeSnapshot] = []
        expected_keys = {
            "sequence",
            "status",
            "version",
            "target_id",
            "target_version",
            "error_code",
            "error_field_code",
        }
        for item in value:
            if not isinstance(item, dict) or set(item) != expected_keys:
                return None
            sequence = item["sequence"]
            status = item["status"]
            version = item["version"]
            target_id_value = item["target_id"]
            target_version = item["target_version"]
            error_code = item["error_code"]
            error_field_code = item["error_field_code"]
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or not isinstance(status, str)
                or not isinstance(version, int)
                or isinstance(version, bool)
                or (
                    target_version is not None
                    and (not isinstance(target_version, int) or isinstance(target_version, bool))
                )
                or (error_code is not None and not isinstance(error_code, str))
                or (error_field_code is not None and not isinstance(error_field_code, str))
            ):
                return None
            try:
                target_id = UUID(target_id_value) if isinstance(target_id_value, str) else None
            except ValueError:
                return None
            if target_id_value is not None and target_id is None:
                return None
            snapshots.append(
                ImportUnitOutcomeSnapshot(
                    sequence=sequence,
                    status=status,
                    version=version,
                    target_id=target_id,
                    target_version=target_version,
                    error_code=error_code,
                    error_field_code=error_field_code,
                )
            )
        return tuple(snapshots)


class SqlAlchemyMasterDataImportRepository(SqlAlchemyMasterDataImportRecoveryReader):
    """在调用方事务内维护批次和原子单元，不提交、不创建连接。"""

    def __init__(self, session: Session) -> None:
        super().__init__(session)

    def add_batch(self, batch: MasterDataImportBatch) -> None:
        """加入新导入批次并 flush，但不提交事务。"""

        self._session.add(batch)
        self._session.flush()

    def add_unit(self, unit: MasterDataImportUnit) -> None:
        """加入新导入原子单元并 flush，但不提交事务。"""

        self._session.add(unit)
        self._session.flush()

    def add_outcome(self, outcome: MasterDataImportCommandOutcome) -> None:
        """加入不可变命令结果并 flush，但不提交事务。"""

        self._session.add(outcome)
        self._session.flush()

    def add_takeover(self, takeover: MasterDataImportTakeover) -> None:
        """追加不可变接管事实并 flush，但不提交事务。"""

        self._session.add(takeover)
        self._session.flush()

    def find_active_idempotency_record_id(
        self,
        school_id: SchoolId,
        batch_id: UUID,
    ) -> UUID | None:
        """无锁预读活动命令标识；调用方必须在后续批次行锁内重新核对。"""

        return self._session.scalar(
            select(MasterDataImportBatch.active_idempotency_record_id).where(
                MasterDataImportBatch.school_id == school_id,
                MasterDataImportBatch.id == batch_id,
            )
        )

    def lock_takeover_source(
        self,
        school_id: SchoolId,
        batch_id: UUID,
        record_id: UUID,
    ) -> IdempotencyRecordOwnerProjection | None:
        """调用固定 definer 函数，只锁定批次当前活动的旧幂等行。"""

        row = (
            self._session.execute(
                text(
                    "SELECT * FROM app.lock_master_data_import_takeover_source("
                    ":school_id, :batch_id, :record_id)"
                ),
                {
                    "school_id": school_id,
                    "batch_id": batch_id,
                    "record_id": record_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return IdempotencyRecordOwnerProjection(
            record_id=row["record_id"],
            status=row["status"],
            request_sha256=row["request_sha256"],
            result_type=row["result_type"],
            result=None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            action_code=row["action_code"],
            actor_user_id=row["actor_user_id"],
            actor_person_id=PersonId(row["actor_person_id"]),
            actor_context_type=row["actor_context_type"],
            actor_assignment_id=AssignmentId(row["actor_assignment_id"]),
            actor_assignment_version=row["actor_assignment_version"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            idempotency_key_hash=row["idempotency_key_hash"],
        )

    def lock_takeover_assignment(
        self,
        school_id: SchoolId,
        batch_id: UUID,
        record_id: UUID,
        assignment_id: AssignmentId,
    ) -> LockedTakeoverAssignment | None:
        """调用固定 definer 函数，只锁定旧命令引用的原任职行。"""

        row = (
            self._session.execute(
                text(
                    "SELECT * FROM app.lock_master_data_import_takeover_assignment("
                    ":school_id, :batch_id, :record_id, :assignment_id)"
                ),
                {
                    "school_id": school_id,
                    "batch_id": batch_id,
                    "record_id": record_id,
                    "assignment_id": assignment_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return LockedTakeoverAssignment(
            assignment_id=AssignmentId(row["assignment_id"]),
            person_id=PersonId(row["person_id"]),
            version=row["version"],
            status=row["status"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
        )

    def supersede_takeover_source(
        self,
        school_id: SchoolId,
        batch_id: UUID,
        record_id: UUID,
        *,
        expected_request_sha256: Sha256Hex,
        batch_version_before: int,
    ) -> None:
        """调用固定 definer 终验、CAS 和审计；调用者不能提供结果或自由文本。"""

        updated = self._session.execute(
            text(
                "SELECT app.supersede_master_data_import_takeover_source("
                ":school_id, :batch_id, :record_id, :request_sha256, :batch_version_before)"
            ),
            {
                "school_id": school_id,
                "batch_id": batch_id,
                "record_id": record_id,
                "request_sha256": expected_request_sha256,
                "batch_version_before": batch_version_before,
            },
        ).scalar_one()
        if updated is not True:
            raise IdempotencyPayloadConflict("旧活动命令已经变化，无法完成接管 CAS。")

    def lock_batch(
        self,
        school_id: SchoolId,
        batch_id: UUID,
    ) -> MasterDataImportBatch | None:
        """按学校和批次标识取得行锁，不扩大到其他学校。"""

        return self._session.scalar(
            select(MasterDataImportBatch)
            .where(
                MasterDataImportBatch.school_id == school_id,
                MasterDataImportBatch.id == batch_id,
            )
            .with_for_update()
        )

    def lock_batch_by_source(
        self,
        school_id: SchoolId,
        *,
        source_kind: str,
        source_ref: str,
        source_sha256: str,
        mode: str,
    ) -> MasterDataImportBatch | None:
        """锁定同一受保护来源的批次，供登记去重使用。"""

        return self._session.scalar(
            select(MasterDataImportBatch)
            .where(
                MasterDataImportBatch.school_id == school_id,
                MasterDataImportBatch.source_kind == source_kind,
                MasterDataImportBatch.source_ref == source_ref,
                MasterDataImportBatch.source_sha256 == source_sha256,
                MasterDataImportBatch.mode == mode,
            )
            .with_for_update()
        )

    def lock_units(
        self,
        school_id: SchoolId,
        batch_id: UUID,
    ) -> tuple[MasterDataImportUnit, ...]:
        """按 sequence 固定顺序锁定批次全部单元，降低死锁风险。"""

        return tuple(
            self._session.scalars(
                select(MasterDataImportUnit)
                .where(
                    MasterDataImportUnit.school_id == school_id,
                    MasterDataImportUnit.batch_id == batch_id,
                )
                .order_by(MasterDataImportUnit.sequence)
                .with_for_update()
            ).all()
        )

    def lock_unit(
        self,
        school_id: SchoolId,
        batch_id: UUID,
        sequence: int,
    ) -> MasterDataImportUnit | None:
        """锁定批次内指定序号的单元，不跨学校读取。"""

        return self._session.scalar(
            select(MasterDataImportUnit)
            .where(
                MasterDataImportUnit.school_id == school_id,
                MasterDataImportUnit.batch_id == batch_id,
                MasterDataImportUnit.sequence == sequence,
            )
            .with_for_update()
        )

    def lock_takeover_by_previous_record(
        self,
        school_id: SchoolId,
        previous_record_id: UUID,
    ) -> MasterDataImportTakeover | None:
        """以事务级 advisory key 串行 previous-record 唯一键，再重验既有事实。"""

        lock_key = (
            f"master_data.import.takeover.previous-record\x1f{school_id}\x1f{previous_record_id}"
        )
        self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 8_608_009)))
        )
        return self._session.scalar(
            select(MasterDataImportTakeover).where(
                MasterDataImportTakeover.school_id == school_id,
                MasterDataImportTakeover.previous_idempotency_record_id == previous_record_id,
            )
        )

    def save_batch(self, batch: MasterDataImportBatch) -> None:
        """flush 已校验的批次变化，但不提交事务。"""

        self._session.add(batch)
        self._session.flush()

    def save_unit(self, unit: MasterDataImportUnit) -> None:
        """flush 已校验的单元变化，但不提交事务。"""

        self._session.add(unit)
        self._session.flush()

    def external_reference_exists(
        self,
        school_id: SchoolId,
        external_reference: str,
    ) -> bool:
        """只查询当前学校已确认姓名事实中的稳定外部编号。"""

        return bool(
            self._session.scalar(
                select(
                    exists().where(
                        SchoolPersonName.school_id == school_id,
                        SchoolPersonName.source_kind == "person_external_reference",
                        SchoolPersonName.source_ref == external_reference,
                        SchoolPersonName.status == "confirmed",
                    )
                )
            )
        )

    def same_display_name_exists(
        self,
        school_id: SchoolId,
        display_name: str,
    ) -> bool:
        """只查询当前学校已确认姓名事实中的同名记录。"""

        return bool(
            self._session.scalar(
                select(
                    exists().where(
                        SchoolPersonName.school_id == school_id,
                        SchoolPersonName.display_name == display_name,
                        SchoolPersonName.status == "confirmed",
                    )
                )
            )
        )

    def lock_display_name_candidate(
        self,
        school_id: SchoolId,
        display_name: str,
    ) -> None:
        """用事务级 advisory lock 串行同校同名检查与正式写入。"""

        lock_key = f"{school_id}\x1f{display_name}"
        self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )
