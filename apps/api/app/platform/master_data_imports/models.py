from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SchoolFactSource(Base):
    """保存学校事实的稳定来源身份；行级写核由后续阶段实现。"""

    __tablename__ = "school_fact_sources"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_school_fact_sources_school_id_id"),
        UniqueConstraint(
            "school_id",
            "source_key",
            name="uq_school_fact_sources_school_source_key",
        ),
        CheckConstraint(
            "source_kind IN ('manual', 'excel', 'system', 'legacy', 'package')",
            name="ck_school_fact_sources_kind",
        ),
        CheckConstraint(
            "schema_version >= 1",
            name="ck_school_fact_sources_schema_version_positive",
        ),
        Index(
            "ix_school_fact_sources_lookup",
            "school_id",
            "source_kind",
            "source_ref",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("schools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    schema_version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SchoolFactInitialization(Base):
    """One closeable external initialization, never an ordinary staff authority."""

    __tablename__ = "school_fact_initializations"
    __table_args__ = (
        UniqueConstraint("school_id", name="uq_school_fact_initializations_school"),
        UniqueConstraint("school_id", "id", name="uq_school_fact_initializations_school_id"),
        UniqueConstraint("request_id", name="uq_school_fact_initializations_request"),
        CheckConstraint(
            "state IN ('prepared', 'applied', 'closed')",
            name="ck_school_fact_initializations_state",
        ),
        CheckConstraint(
            "manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_school_fact_initializations_digest"
        ),
        CheckConstraint(
            "confirmation_token_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_school_fact_initializations_token",
        ),
        CheckConstraint(
            "authority_reference = 'deployment:campus_ai_migration'",
            name="ck_school_fact_initializations_authority",
        ),
        CheckConstraint(
            "(state = 'prepared' AND applied_at IS NULL AND closed_at IS NULL "
            "AND result_payload IS NULL) OR "
            "(state = 'applied' AND applied_at IS NOT NULL AND closed_at IS NULL "
            "AND result_payload IS NOT NULL) OR "
            "(state = 'closed' AND applied_at IS NOT NULL AND closed_at IS NOT "
            "NULL AND result_payload IS NOT NULL)",
            name="ck_school_fact_initializations_result",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("schools.id", ondelete="RESTRICT"), nullable=False
    )
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    authority_reference: Mapped[str] = mapped_column(String(80), nullable=False)
    operator_reference: Mapped[str] = mapped_column(String(1000), nullable=False)
    approval_reference: Mapped[str] = mapped_column(String(1000), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    confirmation_token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    result_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MasterDataImportBatch(Base):
    """保存来源、首次确认身份和计数；数据库会冻结整行终态。"""

    __tablename__ = "master_data_import_batches"
    # Schema21-only control metadata is deliberately excluded from ordinary schema20 ORM SQL.
    # The external initializer inserts it through this Table before using the same Writer.
    __mapper_args__ = {"exclude_properties": ("initialization_id",)}
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "id",
            name="uq_master_data_import_batches_school_id_id",
        ),
        UniqueConstraint(
            "school_id",
            "source_kind",
            "source_ref",
            "source_sha256",
            "mode",
            name="uq_master_data_import_batches_source_identity",
        ),
        UniqueConstraint(
            "confirmation_token_sha256",
            name="uq_master_data_import_batches_confirmation_token",
        ),
        ForeignKeyConstraint(
            ["school_id", "initialization_id"],
            ["school_fact_initializations.school_id", "school_fact_initializations.id"],
            name="fk_master_data_import_batches_initialization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "confirmed_by_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_master_data_import_batches_confirmer_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "confirmed_by_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_master_data_import_batches_confirmer_assignment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "staged_by_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_master_data_import_batches_staged_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "staged_by_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_master_data_import_batches_staged_assignment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "active_idempotency_record_id"],
            ["command_idempotency_records.school_id", "command_idempotency_records.id"],
            name="fk_master_data_import_batches_active_idempotency",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "mode IN ('manual_unit', 'ordinary_atomic_units', 'authoritative_set_publish', "
            "'canonical_atomic_changes')",
            name="ck_master_data_import_batches_mode",
        ),
        CheckConstraint(
            "source_kind IN ('manual', 'excel', 'package')",
            name="ck_master_data_import_batches_source_kind",
        ),
        CheckConstraint(
            "status IN ('source_registered', 'mapped', 'preflight_failed', "
            "'ready_for_confirmation', 'applying', 'partially_failed', "
            "'applied', 'published')",
            name="ck_master_data_import_batches_status",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_master_data_import_batches_source_sha256",
        ),
        CheckConstraint(
            "(source_kind = 'manual' AND filename_summary IS NULL "
            "AND source_size_bytes IS NULL) OR "
            "(source_kind = 'excel' AND filename_summary IS NOT NULL "
            "AND source_size_bytes BETWEEN 1 AND 10485760) OR "
            "(source_kind = 'package' AND filename_summary = 'school-facts-package' "
            "AND source_size_bytes BETWEEN 1 AND 10485760)",
            name="ck_master_data_import_batches_source_metadata",
        ),
        CheckConstraint(
            "planned_units >= 1 AND succeeded_units >= 0 AND failed_units >= 0 "
            "AND pending_units >= 0 "
            "AND planned_units = succeeded_units + failed_units + pending_units",
            name="ck_master_data_import_batches_counts",
        ),
        CheckConstraint(
            "(initialization_id IS NULL AND ((status IN ('source_registered', "
            "'mapped', 'preflight_failed', "
            "'ready_for_confirmation') AND confirmed_by_user_id IS NULL "
            "AND confirmed_by_person_id IS NULL AND confirmed_by_assignment_id IS NULL "
            "AND confirmed_at IS NULL) OR "
            "(status IN ('applying', 'partially_failed', 'applied', 'published') "
            "AND confirmed_by_user_id IS NOT NULL AND confirmed_by_person_id IS NOT NULL "
            "AND confirmed_by_assignment_id IS NOT NULL AND confirmed_at IS NOT NULL))) OR "
            "(initialization_id IS NOT NULL AND confirmed_by_user_id IS NULL "
            "AND confirmed_by_person_id IS NULL AND confirmed_by_assignment_id "
            "IS NULL AND confirmed_at IS NULL)",
            name="ck_master_data_import_batches_confirmation_state",
        ),
        CheckConstraint(
            "mapping_version IS NULL OR mapping_version >= 1",
            name="ck_master_data_import_batches_mapping_version_positive",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_master_data_import_batches_version_positive",
        ),
        CheckConstraint(
            "idempotency_key_hash IS NULL OR idempotency_key_hash ~ '^[0-9a-f]{64}$'",
            name="ck_master_data_import_batches_idempotency_hash",
        ),
        CheckConstraint(
            "(active_idempotency_record_id IS NULL AND active_action_code IS NULL) OR "
            "(active_idempotency_record_id IS NOT NULL AND active_action_code IN "
            "('master_data.import.ordinary_confirm', 'master_data.import.ordinary_resume', "
            "'master_data.import.ordinary_takeover'))",
            name="ck_master_data_import_batches_active_command",
        ),
        CheckConstraint(
            "(mode <> 'canonical_atomic_changes' AND initialization_id IS NULL "
            "AND plan_sha256 IS NULL "
            "AND base_fact_version IS NULL AND target_fact_version IS NULL "
            "AND confirmation_token_sha256 IS NULL AND confirmation_expires_at IS NULL "
            "AND confirmation_consumed_at IS NULL AND confirmation_session_sha256 IS NULL "
            "AND staged_by_user_id IS NULL AND staged_by_person_id IS NULL "
            "AND staged_by_assignment_id IS NULL AND staged_assignment_version IS NULL "
            "AND staged_authorization_version IS NULL AND result_payload IS NULL) OR "
            "(mode = 'canonical_atomic_changes' AND plan_sha256 ~ '^[0-9a-f]{64}$' "
            "AND base_fact_version >= 1 AND target_fact_version >= base_fact_version "
            "AND confirmation_token_sha256 ~ '^[0-9a-f]{64}$' "
            "AND confirmation_expires_at IS NOT NULL "
            "AND confirmation_session_sha256 ~ '^[0-9a-f]{64}$' "
            "AND ((initialization_id IS NULL AND staged_by_user_id IS NOT NULL "
            "AND staged_by_person_id IS NOT NULL "
            "AND staged_by_assignment_id IS NOT NULL AND staged_assignment_version >= 1 "
            "AND staged_authorization_version >= 1) OR (initialization_id IS NOT NULL "
            "AND staged_by_user_id IS NULL AND staged_by_person_id IS NULL AND "
            "staged_by_assignment_id IS NULL "
            "AND staged_assignment_version IS NULL AND staged_authorization_version IS NULL)))",
            name="ck_master_data_import_batches_canonical_state",
        ),
        Index(
            "ix_master_data_import_batches_school_status_created",
            "school_id",
            "status",
            "created_at",
            "id",
        ),
    )

    initialization_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("schools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(String(40), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    filename_summary: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    worksheet_names: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    mapping_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mapping_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    planned_units: Mapped[int] = mapped_column(Integer, nullable=False)
    succeeded_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_units: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmation_card_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_by_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    confirmed_by_person_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    confirmed_by_assignment_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    active_idempotency_record_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    active_action_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    plan_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    base_fact_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_fact_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confirmation_token_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmation_consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmation_session_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    staged_by_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    staged_by_person_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    staged_by_assignment_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    staged_assignment_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    staged_authorization_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    result_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MasterDataImportUnit(Base):
    """保存一个可独立预检、确认、失败和精确续跑的原子导入单元。"""

    __tablename__ = "master_data_import_units"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "id",
            name="uq_master_data_import_units_school_id_id",
        ),
        UniqueConstraint(
            "school_id",
            "batch_id",
            "sequence",
            name="uq_master_data_import_units_batch_sequence",
        ),
        ForeignKeyConstraint(
            ["school_id", "batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_master_data_import_units_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "last_attempt_idempotency_record_id"],
            ["command_idempotency_records.school_id", "command_idempotency_records.id"],
            name="fk_master_data_import_units_last_attempt_idempotency",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "unit_kind IN ('person', 'calendar_set', 'school_fact')",
            name="ck_master_data_import_units_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'preflight_failed', 'ready', 'applying', "
            "'applied', 'failed', 'published')",
            name="ck_master_data_import_units_status",
        ),
        CheckConstraint(
            "sequence >= 1",
            name="ck_master_data_import_units_sequence_positive",
        ),
        CheckConstraint(
            "row_number >= 1",
            name="ck_master_data_import_units_row_number_positive",
        ),
        CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_master_data_import_units_payload_sha256",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_master_data_import_units_version_positive",
        ),
        CheckConstraint(
            "(target_id IS NULL AND target_version IS NULL) OR "
            "(target_id IS NOT NULL AND target_version >= 1)",
            name="ck_master_data_import_units_target_pair",
        ),
        CheckConstraint(
            "(status IN ('preflight_failed', 'failed') AND error_code IS NOT NULL) OR "
            "(status NOT IN ('preflight_failed', 'failed') AND error_code IS NULL)",
            name="ck_master_data_import_units_error_state",
        ),
        CheckConstraint(
            "(last_attempt_idempotency_record_id IS NULL AND last_attempt_completed IS NULL) OR "
            "(last_attempt_idempotency_record_id IS NOT NULL "
            "AND last_attempt_completed IS NOT NULL)",
            name="ck_master_data_import_units_attempt_state",
        ),
        Index(
            "ix_master_data_import_units_batch_status_sequence",
            "school_id",
            "batch_id",
            "status",
            "sequence",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    batch_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    soft_anomalies: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    initial_soft_anomalies: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    decision_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    target_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    target_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_field_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_attempt_idempotency_record_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    last_attempt_completed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SchoolFactChangeLineage(Base):
    """把规范变更行绑定到复用的导入批次/单元与最终事实版本。"""

    __tablename__ = "school_fact_change_lineage"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_school_fact_change_lineage_school_id"),
        UniqueConstraint(
            "school_id",
            "batch_id",
            "unit_id",
            name="uq_school_fact_change_lineage_unit",
        ),
        UniqueConstraint(
            "school_id",
            "fact_kind",
            "fact_source_key",
            "target_version",
            name="uq_school_fact_change_lineage_fact_version",
        ),
        ForeignKeyConstraint(
            ["school_id", "batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_school_fact_change_lineage_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "unit_id"],
            ["master_data_import_units.school_id", "master_data_import_units.id"],
            name="fk_school_fact_change_lineage_unit",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "operation IN ('upsert', 'end', 'revoke', 'reactivate', 'correct', 'cancel')",
            name="ck_school_fact_change_lineage_operation",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$' AND target_version >= 1 AND row_number >= 1",
            name="ck_school_fact_change_lineage_values",
        ),
        Index(
            "ix_school_fact_change_lineage_target",
            "school_id",
            "fact_kind",
            "fact_source_key",
            "target_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    batch_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    unit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    fact_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    fact_source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    target_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sheet_code: Mapped[str] = mapped_column(String(120), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MasterDataImportTakeover(Base):
    """只追加保存原活动命令到新命令的显式人工接管事实。"""

    __tablename__ = "master_data_import_takeovers"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "id",
            name="uq_master_data_import_takeovers_school_id_id",
        ),
        UniqueConstraint(
            "school_id",
            "previous_idempotency_record_id",
            name="uq_master_data_import_takeovers_previous_command",
        ),
        UniqueConstraint(
            "school_id",
            "new_idempotency_record_id",
            name="uq_master_data_import_takeovers_new_command",
        ),
        ForeignKeyConstraint(
            ["school_id", "batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_master_data_import_takeovers_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "previous_idempotency_record_id"],
            ["command_idempotency_records.school_id", "command_idempotency_records.id"],
            name="fk_master_data_import_takeovers_previous_idempotency",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "new_idempotency_record_id"],
            ["command_idempotency_records.school_id", "command_idempotency_records.id"],
            name="fk_master_data_import_takeovers_new_idempotency",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "original_actor_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_master_data_import_takeovers_original_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "original_actor_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_master_data_import_takeovers_original_assignment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "new_actor_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_master_data_import_takeovers_new_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "new_actor_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_master_data_import_takeovers_new_assignment",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "original_actor_assignment_version >= 1",
            name="ck_master_data_import_takeovers_original_version",
        ),
        CheckConstraint(
            "new_actor_assignment_version >= 1",
            name="ck_master_data_import_takeovers_new_version",
        ),
        CheckConstraint(
            "batch_version_before >= 1 AND batch_version_after = batch_version_before + 1",
            name="ck_master_data_import_takeovers_batch_versions",
        ),
        CheckConstraint(
            "btrim(takeover_reason) <> ''",
            name="ck_master_data_import_takeovers_reason_non_empty",
        ),
        CheckConstraint(
            "previous_idempotency_record_id <> new_idempotency_record_id",
            name="ck_master_data_import_takeovers_distinct_commands",
        ),
        CheckConstraint(
            "cardinality(transferred_sequences) BETWEEN 1 AND 500 "
            "AND cardinality(transferred_sequences) = "
            "cardinality(transferred_unit_versions)",
            name="ck_master_data_import_takeovers_manifest_cardinality",
        ),
        CheckConstraint(
            "array_position(transferred_sequences, NULL) IS NULL "
            "AND array_position(transferred_unit_versions, NULL) IS NULL "
            "AND 0 < ALL(transferred_sequences) "
            "AND 0 < ALL(transferred_unit_versions)",
            name="ck_master_data_import_takeovers_manifest_positive",
        ),
        Index(
            "ix_master_data_import_takeovers_batch_created",
            "school_id",
            "batch_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("schools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    batch_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    previous_idempotency_record_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    new_idempotency_record_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    original_actor_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    original_actor_person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    original_actor_assignment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    original_actor_assignment_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    new_actor_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    new_actor_person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    new_actor_assignment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    new_actor_assignment_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    takeover_reason: Mapped[str] = mapped_column(String(500), nullable=False)
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    batch_version_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    batch_version_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    transferred_sequences: Mapped[list[int]] = mapped_column(
        ARRAY(Integer),
        nullable=False,
        server_default=FetchedValue(),
    )
    transferred_unit_versions: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger),
        nullable=False,
        server_default=FetchedValue(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class MasterDataImportCommandOutcome(Base):
    """不可变保存安全序号结果，并由延后约束核对批次与幂等终态。"""

    __tablename__ = "master_data_import_command_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "id",
            name="uq_master_data_import_command_outcomes_school_id_id",
        ),
        UniqueConstraint(
            "school_id",
            "idempotency_record_id",
            name="uq_master_data_import_command_outcomes_idempotency",
        ),
        ForeignKeyConstraint(
            ["school_id", "batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_master_data_import_command_outcomes_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "idempotency_record_id"],
            ["command_idempotency_records.school_id", "command_idempotency_records.id"],
            name="fk_master_data_import_command_outcomes_idempotency",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "action_code IN ('master_data.import.manual_confirm', "
            "'master_data.import.ordinary_confirm', 'master_data.import.ordinary_resume', "
            "'master_data.import.ordinary_takeover', "
            "'master_data.import.calendar_publish', 'school_facts.confirm')",
            name="ck_master_data_import_command_outcomes_action",
        ),
        CheckConstraint(
            "status IN ('applied', 'partially_failed', 'published')",
            name="ck_master_data_import_command_outcomes_status",
        ),
        CheckConstraint(
            "batch_version >= 1",
            name="ck_master_data_import_command_outcomes_batch_version_positive",
        ),
        CheckConstraint(
            "cardinality(succeeded_units) <= 50000 "
            "AND cardinality(failed_units) <= 50000 "
            "AND cardinality(executed_units) <= 50000",
            name="ck_master_data_import_command_outcomes_unit_limits",
        ),
        CheckConstraint(
            "unit_outcome_snapshots IS NULL OR jsonb_typeof(unit_outcome_snapshots) = 'array'",
            name="ck_master_data_import_command_outcomes_unit_snapshots_array",
        ),
        Index(
            "ix_master_data_import_command_outcomes_lookup",
            "school_id",
            "idempotency_record_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    idempotency_record_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    batch_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    action_code: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    batch_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    succeeded_units: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)
    failed_units: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)
    executed_units: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)
    unit_outcome_snapshots: Mapped[list[dict[str, object]] | None] = mapped_column(
        JSONB,
        nullable=True,
        server_default=FetchedValue(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
