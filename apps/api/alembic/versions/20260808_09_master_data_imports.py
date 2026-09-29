"""Add tenant-isolated master-data import drafts.

Revision ID: 20260808_09
Revises: 20260808_08
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260808_09"
down_revision: str | None = "20260808_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IMPORT_TABLES = (
    "master_data_import_batches",
    "master_data_import_units",
    "master_data_import_command_outcomes",
    "master_data_import_takeovers",
)
_RUNTIME_UPDATE_COLUMNS = {
    "master_data_import_batches": (
        "status",
        "succeeded_units",
        "failed_units",
        "pending_units",
        "confirmation_card_sha256",
        "confirmed_by_user_id",
        "confirmed_by_person_id",
        "confirmed_by_assignment_id",
        "confirmed_at",
        "active_idempotency_record_id",
        "active_action_code",
        "version",
        "updated_at",
    ),
    "master_data_import_units": (
        "status",
        "soft_anomalies",
        "decision_reason",
        "target_id",
        "target_version",
        "error_code",
        "error_field_code",
        "last_attempt_idempotency_record_id",
        "last_attempt_completed",
        "version",
        "updated_at",
    ),
}


def _uuid_column(name: str, *, nullable: bool = False) -> sa.Column[object]:
    return sa.Column(name, postgresql.UUID(as_uuid=True), nullable=nullable)


def _version_column() -> sa.Column[object]:
    return sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False)


def _created_at_column() -> sa.Column[object]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


def _updated_at_column() -> sa.Column[object]:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


def _create_import_tables() -> None:
    """由数据库 Owner 创建批次、原子单元和不可变命令结果表。"""

    op.create_unique_constraint(
        "uq_command_idempotency_records_school_id_id",
        "command_idempotency_records",
        ["school_id", "id"],
    )

    op.create_table(
        "master_data_import_batches",
        _uuid_column("id"),
        _uuid_column("school_id"),
        sa.Column("mode", sa.String(length=40), nullable=False),
        sa.Column("source_kind", sa.String(length=20), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("filename_summary", sa.String(length=255), nullable=True),
        sa.Column("source_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "worksheet_names",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("mapping_version", sa.BigInteger(), nullable=True),
        sa.Column(
            "mapping_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("planned_units", sa.Integer(), nullable=False),
        sa.Column("succeeded_units", sa.Integer(), nullable=False),
        sa.Column("failed_units", sa.Integer(), nullable=False),
        sa.Column("pending_units", sa.Integer(), nullable=False),
        sa.Column("confirmation_card_sha256", sa.String(length=64), nullable=True),
        sa.Column("confirmed_by_user_id", sa.Integer(), nullable=True),
        _uuid_column("confirmed_by_person_id", nullable=True),
        _uuid_column("confirmed_by_assignment_id", nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        _uuid_column("request_id"),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        _uuid_column("active_idempotency_record_id", nullable=True),
        sa.Column("active_action_code", sa.String(length=80), nullable=True),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_master_data_import_batches"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_master_data_import_batches_school_id_id",
        ),
        sa.UniqueConstraint(
            "school_id",
            "source_kind",
            "source_ref",
            "source_sha256",
            "mode",
            name="uq_master_data_import_batches_source_identity",
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_master_data_import_batches_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by_user_id"],
            ["users.id"],
            name="fk_master_data_import_batches_confirmer_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "confirmed_by_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_master_data_import_batches_confirmer_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "confirmed_by_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_master_data_import_batches_confirmer_assignment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "active_idempotency_record_id"],
            ["command_idempotency_records.school_id", "command_idempotency_records.id"],
            name="fk_master_data_import_batches_active_idempotency",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "mode IN ('manual_unit', 'ordinary_atomic_units', 'authoritative_set_publish')",
            name="ck_master_data_import_batches_mode",
        ),
        sa.CheckConstraint(
            "source_kind IN ('manual', 'excel')",
            name="ck_master_data_import_batches_source_kind",
        ),
        sa.CheckConstraint(
            "status IN ('source_registered', 'mapped', 'preflight_failed', "
            "'ready_for_confirmation', 'applying', 'partially_failed', "
            "'applied', 'published')",
            name="ck_master_data_import_batches_status",
        ),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_master_data_import_batches_source_sha256",
        ),
        sa.CheckConstraint(
            "(source_kind = 'manual' AND filename_summary IS NULL "
            "AND source_size_bytes IS NULL) OR "
            "(source_kind = 'excel' AND filename_summary IS NOT NULL "
            "AND source_size_bytes BETWEEN 1 AND 10485760)",
            name="ck_master_data_import_batches_source_metadata",
        ),
        sa.CheckConstraint(
            "planned_units >= 1 AND succeeded_units >= 0 AND failed_units >= 0 "
            "AND pending_units >= 0 "
            "AND planned_units = succeeded_units + failed_units + pending_units",
            name="ck_master_data_import_batches_counts",
        ),
        sa.CheckConstraint(
            "(status IN ('source_registered', 'mapped', 'preflight_failed', "
            "'ready_for_confirmation') AND confirmed_by_user_id IS NULL "
            "AND confirmed_by_person_id IS NULL AND confirmed_by_assignment_id IS NULL "
            "AND confirmed_at IS NULL) OR "
            "(status IN ('applying', 'partially_failed', 'applied', 'published') "
            "AND confirmed_by_user_id IS NOT NULL AND confirmed_by_person_id IS NOT NULL "
            "AND confirmed_by_assignment_id IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="ck_master_data_import_batches_confirmation_state",
        ),
        sa.CheckConstraint(
            "mapping_version IS NULL OR mapping_version >= 1",
            name="ck_master_data_import_batches_mapping_version_positive",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_master_data_import_batches_version_positive",
        ),
        sa.CheckConstraint(
            "idempotency_key_hash IS NULL OR idempotency_key_hash ~ '^[0-9a-f]{64}$'",
            name="ck_master_data_import_batches_idempotency_hash",
        ),
        sa.CheckConstraint(
            "(active_idempotency_record_id IS NULL AND active_action_code IS NULL) OR "
            "(active_idempotency_record_id IS NOT NULL AND active_action_code IN "
            "('master_data.import.ordinary_confirm', 'master_data.import.ordinary_resume', "
            "'master_data.import.ordinary_takeover'))",
            name="ck_master_data_import_batches_active_command",
        ),
    )
    op.create_index(
        "ix_master_data_import_batches_school_status_created",
        "master_data_import_batches",
        ["school_id", "status", "created_at", "id"],
    )

    op.create_table(
        "master_data_import_units",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("batch_id"),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("unit_kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "soft_anomalies",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "initial_soft_anomalies",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("decision_reason", sa.String(length=500), nullable=True),
        _uuid_column("target_id", nullable=True),
        sa.Column("target_version", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_field_code", sa.String(length=80), nullable=True),
        _uuid_column("last_attempt_idempotency_record_id", nullable=True),
        sa.Column("last_attempt_completed", sa.Boolean(), nullable=True),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_master_data_import_units"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_master_data_import_units_school_id_id",
        ),
        sa.UniqueConstraint(
            "school_id",
            "batch_id",
            "sequence",
            name="uq_master_data_import_units_batch_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_master_data_import_units_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "last_attempt_idempotency_record_id"],
            ["command_idempotency_records.school_id", "command_idempotency_records.id"],
            name="fk_master_data_import_units_last_attempt_idempotency",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "unit_kind IN ('person', 'calendar_set')",
            name="ck_master_data_import_units_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'preflight_failed', 'ready', 'applying', "
            "'applied', 'failed', 'published')",
            name="ck_master_data_import_units_status",
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_master_data_import_units_sequence_positive",
        ),
        sa.CheckConstraint(
            "row_number >= 1",
            name="ck_master_data_import_units_row_number_positive",
        ),
        sa.CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_master_data_import_units_payload_sha256",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_master_data_import_units_version_positive",
        ),
        sa.CheckConstraint(
            "(target_id IS NULL AND target_version IS NULL) OR "
            "(target_id IS NOT NULL AND target_version >= 1)",
            name="ck_master_data_import_units_target_pair",
        ),
        sa.CheckConstraint(
            "(status IN ('preflight_failed', 'failed') AND error_code IS NOT NULL) OR "
            "(status NOT IN ('preflight_failed', 'failed') AND error_code IS NULL)",
            name="ck_master_data_import_units_error_state",
        ),
        sa.CheckConstraint(
            "(last_attempt_idempotency_record_id IS NULL AND last_attempt_completed IS NULL) OR "
            "(last_attempt_idempotency_record_id IS NOT NULL "
            "AND last_attempt_completed IS NOT NULL)",
            name="ck_master_data_import_units_attempt_state",
        ),
    )
    op.create_index(
        "ix_master_data_import_units_batch_status_sequence",
        "master_data_import_units",
        ["school_id", "batch_id", "status", "sequence"],
    )

    op.create_table(
        "master_data_import_command_outcomes",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("idempotency_record_id"),
        _uuid_column("batch_id"),
        sa.Column("action_code", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("batch_version", sa.BigInteger(), nullable=False),
        sa.Column("succeeded_units", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("failed_units", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("executed_units", postgresql.ARRAY(sa.Integer()), nullable=False),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_master_data_import_command_outcomes"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_master_data_import_command_outcomes_school_id_id",
        ),
        sa.UniqueConstraint(
            "school_id",
            "idempotency_record_id",
            name="uq_master_data_import_command_outcomes_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_master_data_import_command_outcomes_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "idempotency_record_id"],
            ["command_idempotency_records.school_id", "command_idempotency_records.id"],
            name="fk_master_data_import_command_outcomes_idempotency",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "action_code IN ('master_data.import.manual_confirm', "
            "'master_data.import.ordinary_confirm', 'master_data.import.ordinary_resume', "
            "'master_data.import.ordinary_takeover', "
            "'master_data.import.calendar_publish')",
            name="ck_master_data_import_command_outcomes_action",
        ),
        sa.CheckConstraint(
            "status IN ('applied', 'partially_failed', 'published')",
            name="ck_master_data_import_command_outcomes_status",
        ),
        sa.CheckConstraint(
            "batch_version >= 1",
            name="ck_master_data_import_command_outcomes_batch_version_positive",
        ),
        sa.CheckConstraint(
            "cardinality(succeeded_units) <= 500 "
            "AND cardinality(failed_units) <= 500 "
            "AND cardinality(executed_units) <= 500",
            name="ck_master_data_import_command_outcomes_unit_limits",
        ),
    )
    op.create_index(
        "ix_master_data_import_command_outcomes_lookup",
        "master_data_import_command_outcomes",
        ["school_id", "idempotency_record_id"],
    )

    op.create_table(
        "master_data_import_takeovers",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("batch_id"),
        _uuid_column("previous_idempotency_record_id"),
        _uuid_column("new_idempotency_record_id"),
        sa.Column("original_actor_user_id", sa.Integer(), nullable=False),
        _uuid_column("original_actor_person_id"),
        _uuid_column("original_actor_assignment_id"),
        sa.Column("original_actor_assignment_version", sa.BigInteger(), nullable=False),
        sa.Column("new_actor_user_id", sa.Integer(), nullable=False),
        _uuid_column("new_actor_person_id"),
        _uuid_column("new_actor_assignment_id"),
        sa.Column("new_actor_assignment_version", sa.BigInteger(), nullable=False),
        sa.Column("takeover_reason", sa.String(length=500), nullable=False),
        _uuid_column("request_id"),
        sa.Column("batch_version_before", sa.BigInteger(), nullable=False),
        sa.Column("batch_version_after", sa.BigInteger(), nullable=False),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_master_data_import_takeovers"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_master_data_import_takeovers_school_id_id",
        ),
        sa.UniqueConstraint(
            "school_id",
            "previous_idempotency_record_id",
            name="uq_master_data_import_takeovers_previous_command",
        ),
        sa.UniqueConstraint(
            "school_id",
            "new_idempotency_record_id",
            name="uq_master_data_import_takeovers_new_command",
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_master_data_import_takeovers_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_master_data_import_takeovers_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "previous_idempotency_record_id"],
            ["command_idempotency_records.school_id", "command_idempotency_records.id"],
            name="fk_master_data_import_takeovers_previous_idempotency",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "new_idempotency_record_id"],
            ["command_idempotency_records.school_id", "command_idempotency_records.id"],
            name="fk_master_data_import_takeovers_new_idempotency",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["original_actor_user_id"],
            ["users.id"],
            name="fk_master_data_import_takeovers_original_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "original_actor_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_master_data_import_takeovers_original_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "original_actor_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_master_data_import_takeovers_original_assignment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["new_actor_user_id"],
            ["users.id"],
            name="fk_master_data_import_takeovers_new_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "new_actor_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_master_data_import_takeovers_new_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "new_actor_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_master_data_import_takeovers_new_assignment",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "original_actor_assignment_version >= 1",
            name="ck_master_data_import_takeovers_original_version",
        ),
        sa.CheckConstraint(
            "new_actor_assignment_version >= 1",
            name="ck_master_data_import_takeovers_new_version",
        ),
        sa.CheckConstraint(
            "batch_version_before >= 1 AND batch_version_after = batch_version_before + 1",
            name="ck_master_data_import_takeovers_batch_versions",
        ),
        sa.CheckConstraint(
            "btrim(takeover_reason) <> ''",
            name="ck_master_data_import_takeovers_reason_non_empty",
        ),
        sa.CheckConstraint(
            "previous_idempotency_record_id <> new_idempotency_record_id",
            name="ck_master_data_import_takeovers_distinct_commands",
        ),
    )
    op.create_index(
        "ix_master_data_import_takeovers_batch_created",
        "master_data_import_takeovers",
        ["school_id", "batch_id", "created_at", "id"],
    )


def _enable_rls_and_grant_runtime() -> None:
    """强制租户 RLS，并只授予 runtime 必要的插入和状态列更新权。"""

    owner_bypass = "CURRENT_USER = 'campus_ai_db_owner'"
    tenant_match = "school_id = (SELECT app.require_school_context())"
    command_context = (
        "current_setting('app.context_purpose', true) = 'command' "
        "AND current_setting('app.idempotency_key_hash', true) ~ '^[0-9a-f]{64}$'"
    )
    for table_name in _IMPORT_TABLES:
        op.execute(f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_select ON public.{table_name} FOR SELECT "
            f"USING (CASE WHEN {owner_bypass} THEN true ELSE {tenant_match} END)"
        )
        op.execute(
            f"CREATE POLICY tenant_insert ON public.{table_name} FOR INSERT "
            f"WITH CHECK (CASE WHEN {owner_bypass} THEN true "
            f"ELSE {tenant_match} AND {command_context} END)"
        )
        op.execute(
            f"CREATE POLICY tenant_update ON public.{table_name} FOR UPDATE "
            f"USING (CASE WHEN {owner_bypass} THEN true "
            f"ELSE {tenant_match} AND {command_context} END) "
            f"WITH CHECK (CASE WHEN {owner_bypass} THEN true "
            f"ELSE {tenant_match} AND {command_context} END)"
        )
        op.execute(
            f"CREATE POLICY owner_delete ON public.{table_name} FOR DELETE USING ({owner_bypass})"
        )
        op.execute(f"GRANT SELECT, INSERT ON TABLE public.{table_name} TO campus_ai_runtime")
        op.execute(
            f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.{table_name} FROM campus_ai_runtime"
        )
        update_columns = _RUNTIME_UPDATE_COLUMNS.get(table_name)
        if update_columns is not None:
            op.execute(
                f"GRANT UPDATE ({', '.join(update_columns)}) "
                f"ON TABLE public.{table_name} TO campus_ai_runtime"
            )


def _create_import_takeover_immutability_triggers() -> None:
    """阻止包括表 Owner 在内的普通 DML 改写或清空接管来源事实。"""

    op.execute(
        """
        CREATE FUNCTION app_private.reject_master_data_import_takeover_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION 'master data import takeover provenance is immutable'
                USING ERRCODE = '55000';
        END
        $function$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "app_private.reject_master_data_import_takeover_mutation() FROM PUBLIC"
    )
    op.execute(
        """
        CREATE TRIGGER reject_master_data_import_takeover_row_mutation
        BEFORE UPDATE OR DELETE ON public.master_data_import_takeovers
        FOR EACH ROW
        EXECUTE FUNCTION app_private.reject_master_data_import_takeover_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_master_data_import_takeover_truncate
        BEFORE TRUNCATE ON public.master_data_import_takeovers
        FOR EACH STATEMENT
        EXECUTE FUNCTION app_private.reject_master_data_import_takeover_mutation()
        """
    )


def _create_import_unit_immutability_trigger() -> None:
    """终态单元整行冻结，禁止经状态往返改写人工决定或异常证据。"""

    op.execute(
        """
        CREATE FUNCTION app_private.enforce_master_data_import_unit_immutability()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF OLD.status IN ('applied', 'published') AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'terminal master data import decision is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER enforce_master_data_import_unit_immutability
        BEFORE UPDATE ON public.master_data_import_units
        FOR EACH ROW
        EXECUTE FUNCTION app_private.enforce_master_data_import_unit_immutability()
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "app_private.enforce_master_data_import_unit_immutability() FROM PUBLIC"
    )


def _create_import_batch_guard() -> None:
    """冻结终态批次，并把首次确认归属绑定到可信任职上下文。"""

    op.execute(
        """
        CREATE FUNCTION app_private.enforce_master_data_import_batch_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_validate_confirmer boolean := false;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.status IN ('applied', 'published')
                   AND NEW IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION 'terminal master data import batch is immutable'
                        USING ERRCODE = '55000';
                END IF;

                IF (OLD.confirmed_by_user_id, OLD.confirmed_by_person_id,
                    OLD.confirmed_by_assignment_id, OLD.confirmed_at)
                   IS DISTINCT FROM
                   (NEW.confirmed_by_user_id, NEW.confirmed_by_person_id,
                    NEW.confirmed_by_assignment_id, NEW.confirmed_at) THEN
                    IF OLD.confirmed_by_user_id IS NOT NULL
                       OR OLD.confirmed_by_person_id IS NOT NULL
                       OR OLD.confirmed_by_assignment_id IS NOT NULL
                       OR OLD.confirmed_at IS NOT NULL THEN
                        RAISE EXCEPTION 'master data import confirmer is immutable'
                            USING ERRCODE = '55000';
                    END IF;
                    v_validate_confirmer := true;
                END IF;
            ELSIF NEW.confirmed_by_user_id IS NOT NULL
               OR NEW.confirmed_by_person_id IS NOT NULL
               OR NEW.confirmed_by_assignment_id IS NOT NULL
               OR NEW.confirmed_at IS NOT NULL THEN
                v_validate_confirmer := true;
            END IF;

            IF v_validate_confirmer THEN
                IF NEW.confirmed_by_user_id IS NULL
                   OR NEW.confirmed_by_person_id IS NULL
                   OR NEW.confirmed_by_assignment_id IS NULL
                   OR NEW.confirmed_at IS NULL
                   OR current_setting('app.context_purpose', true)
                      IS DISTINCT FROM 'command'
                   OR current_setting('app.context_kind', true)
                      IS DISTINCT FROM 'assignment'
                   OR NEW.confirmed_by_user_id IS DISTINCT FROM
                      current_setting('app.account_user_id', true)::integer
                   OR NEW.confirmed_by_person_id IS DISTINCT FROM
                      current_setting('app.person_id', true)::uuid
                   OR NEW.confirmed_by_assignment_id IS DISTINCT FROM
                      current_setting('app.assignment_id', true)::uuid
                   OR NOT EXISTS (
                       SELECT 1
                       FROM public.staff_assignments AS assignment
                       WHERE assignment.school_id = NEW.school_id
                         AND assignment.id = NEW.confirmed_by_assignment_id
                         AND assignment.person_id = NEW.confirmed_by_person_id
                         AND assignment.version =
                             current_setting('app.assignment_version', true)::bigint
                         AND assignment.status = 'confirmed'
                         AND assignment.valid_from <= pg_catalog.clock_timestamp()
                         AND (
                             assignment.valid_to IS NULL
                             OR assignment.valid_to > pg_catalog.clock_timestamp()
                         )
                   ) THEN
                    RAISE EXCEPTION 'master data import confirmer context mismatch'
                        USING ERRCODE = '28000';
                END IF;
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER enforce_master_data_import_batch_guard
        BEFORE INSERT OR UPDATE ON public.master_data_import_batches
        FOR EACH ROW
        EXECUTE FUNCTION app_private.enforce_master_data_import_batch_guard()
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION app_private.enforce_master_data_import_batch_guard() FROM PUBLIC"
    )


def _create_terminal_consistency_triggers() -> None:
    """在事务结束时核对终态批次、单元、结果和幂等安全载荷。"""

    op.execute(
        """
        CREATE FUNCTION app_private.verify_master_data_import_terminal_consistency(
            p_school_id uuid,
            p_batch_id uuid
        )
        RETURNS void
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_batch record;
            v_outcome record;
            v_idempotency record;
            v_outcome_count bigint;
            v_succeeded bigint;
            v_failed bigint;
            v_pending bigint;
            v_normalized integer[];
            v_union integer[];
        BEGIN
            SELECT batch.status,
                   batch.version,
                   batch.succeeded_units,
                   batch.failed_units,
                   batch.pending_units
            INTO v_batch
            FROM public.master_data_import_batches AS batch
            WHERE batch.school_id = p_school_id
              AND batch.id = p_batch_id;
            IF NOT FOUND OR v_batch.status NOT IN ('applied', 'published') THEN
                RETURN;
            END IF;

            SELECT count(*) FILTER (WHERE unit.status IN ('applied', 'published')),
                   count(*) FILTER (WHERE unit.status = 'failed'),
                   count(*) FILTER (
                       WHERE unit.status NOT IN ('applied', 'published', 'failed')
                   )
            INTO v_succeeded, v_failed, v_pending
            FROM public.master_data_import_units AS unit
            WHERE unit.school_id = p_school_id
              AND unit.batch_id = p_batch_id;
            IF (v_succeeded, v_failed, v_pending) IS DISTINCT FROM
               (v_batch.succeeded_units, v_batch.failed_units, v_batch.pending_units) THEN
                RAISE EXCEPTION 'master data import terminal counts are inconsistent'
                    USING ERRCODE = '23514';
            END IF;

            SELECT count(*)
            INTO v_outcome_count
            FROM public.master_data_import_command_outcomes AS outcome
            WHERE outcome.school_id = p_school_id
              AND outcome.batch_id = p_batch_id
              AND outcome.batch_version = v_batch.version
              AND outcome.status = v_batch.status;
            IF v_outcome_count <> 1 THEN
                RAISE EXCEPTION 'master data import terminal outcome is missing or ambiguous'
                    USING ERRCODE = '23514';
            END IF;

            SELECT outcome.idempotency_record_id,
                   outcome.status,
                   outcome.batch_version,
                   outcome.succeeded_units,
                   outcome.failed_units,
                   outcome.executed_units
            INTO STRICT v_outcome
            FROM public.master_data_import_command_outcomes AS outcome
            WHERE outcome.school_id = p_school_id
              AND outcome.batch_id = p_batch_id
              AND outcome.batch_version = v_batch.version
              AND outcome.status = v_batch.status;

            SELECT COALESCE(array_agg(value ORDER BY value), '{}'::integer[])
            INTO v_normalized
            FROM (
                SELECT DISTINCT value
                FROM unnest(v_outcome.succeeded_units) AS item(value)
                WHERE value IS NOT NULL
            ) AS normalized;
            IF v_normalized IS DISTINCT FROM v_outcome.succeeded_units THEN
                RAISE EXCEPTION 'master data import succeeded units are not canonical'
                    USING ERRCODE = '23514';
            END IF;

            SELECT COALESCE(array_agg(value ORDER BY value), '{}'::integer[])
            INTO v_normalized
            FROM (
                SELECT DISTINCT value
                FROM unnest(v_outcome.failed_units) AS item(value)
                WHERE value IS NOT NULL
            ) AS normalized;
            IF v_normalized IS DISTINCT FROM v_outcome.failed_units THEN
                RAISE EXCEPTION 'master data import failed units are not canonical'
                    USING ERRCODE = '23514';
            END IF;

            SELECT COALESCE(array_agg(value ORDER BY value), '{}'::integer[])
            INTO v_normalized
            FROM (
                SELECT DISTINCT value
                FROM unnest(v_outcome.executed_units) AS item(value)
                WHERE value IS NOT NULL
            ) AS normalized;
            IF v_normalized IS DISTINCT FROM v_outcome.executed_units THEN
                RAISE EXCEPTION 'master data import executed units are not canonical'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM unnest(v_outcome.succeeded_units) AS succeeded(sequence)
                JOIN unnest(v_outcome.failed_units) AS failed(sequence) USING (sequence)
            ) THEN
                RAISE EXCEPTION 'master data import outcome unit sets overlap'
                    USING ERRCODE = '23514';
            END IF;
            SELECT COALESCE(array_agg(sequence ORDER BY sequence), '{}'::integer[])
            INTO v_union
            FROM (
                SELECT sequence
                FROM unnest(v_outcome.succeeded_units) AS succeeded(sequence)
                UNION
                SELECT sequence
                FROM unnest(v_outcome.failed_units) AS failed(sequence)
            ) AS combined;
            IF v_union IS DISTINCT FROM v_outcome.executed_units THEN
                RAISE EXCEPTION 'master data import executed units do not match outcome sets'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM unnest(v_outcome.succeeded_units) AS expected(sequence)
                LEFT JOIN public.master_data_import_units AS unit
                  ON unit.school_id = p_school_id
                 AND unit.batch_id = p_batch_id
                 AND unit.sequence = expected.sequence
                WHERE unit.id IS NULL OR unit.status NOT IN ('applied', 'published')
            ) OR EXISTS (
                SELECT 1
                FROM unnest(v_outcome.failed_units) AS expected(sequence)
                LEFT JOIN public.master_data_import_units AS unit
                  ON unit.school_id = p_school_id
                 AND unit.batch_id = p_batch_id
                 AND unit.sequence = expected.sequence
                WHERE unit.id IS NULL OR unit.status <> 'failed'
            ) THEN
                RAISE EXCEPTION 'master data import outcome units do not match unit states'
                    USING ERRCODE = '23514';
            END IF;

            SELECT record.status, record.result_type, record.result_payload
            INTO v_idempotency
            FROM public.command_idempotency_records AS record
            WHERE record.school_id = p_school_id
              AND record.id = v_outcome.idempotency_record_id;
            IF NOT FOUND
               OR v_idempotency.status IS DISTINCT FROM 'succeeded'
               OR v_idempotency.result_type IS DISTINCT FROM 'master_data.import_outcome'
               OR v_idempotency.result_payload ->> 'resource_type'
                  IS DISTINCT FROM 'master_data.import_batch'
               OR v_idempotency.result_payload ->> 'resource_id'
                  IS DISTINCT FROM p_batch_id::text
               OR v_idempotency.result_payload ->> 'resource_version'
                  IS DISTINCT FROM v_outcome.batch_version::text
               OR v_idempotency.result_payload ->> 'outcome_code'
                  IS DISTINCT FROM v_outcome.status THEN
                RAISE EXCEPTION 'master data import idempotency result is inconsistent'
                    USING ERRCODE = '23514';
            END IF;
        END
        $function$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "app_private.verify_master_data_import_terminal_consistency(uuid,uuid) FROM PUBLIC"
    )
    op.execute(
        """
        CREATE FUNCTION app_private.enforce_master_data_import_terminal_consistency()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_batch_id uuid;
        BEGIN
            IF TG_TABLE_NAME = 'master_data_import_batches' THEN
                PERFORM app_private.verify_master_data_import_terminal_consistency(
                    NEW.school_id,
                    NEW.id
                );
                IF TG_OP = 'UPDATE'
                   AND (OLD.school_id, OLD.id) IS DISTINCT FROM (NEW.school_id, NEW.id) THEN
                    PERFORM app_private.verify_master_data_import_terminal_consistency(
                        OLD.school_id,
                        OLD.id
                    );
                END IF;
            ELSIF TG_TABLE_NAME = 'master_data_import_units' THEN
                PERFORM app_private.verify_master_data_import_terminal_consistency(
                    NEW.school_id,
                    NEW.batch_id
                );
                IF TG_OP = 'UPDATE'
                   AND (OLD.school_id, OLD.batch_id)
                       IS DISTINCT FROM (NEW.school_id, NEW.batch_id) THEN
                    PERFORM app_private.verify_master_data_import_terminal_consistency(
                        OLD.school_id,
                        OLD.batch_id
                    );
                END IF;
            ELSIF TG_TABLE_NAME = 'master_data_import_command_outcomes' THEN
                PERFORM app_private.verify_master_data_import_terminal_consistency(
                    NEW.school_id,
                    NEW.batch_id
                );
                IF TG_OP = 'UPDATE'
                   AND (OLD.school_id, OLD.batch_id)
                       IS DISTINCT FROM (NEW.school_id, NEW.batch_id) THEN
                    PERFORM app_private.verify_master_data_import_terminal_consistency(
                        OLD.school_id,
                        OLD.batch_id
                    );
                END IF;
            ELSE
                FOR v_batch_id IN
                    SELECT outcome.batch_id
                    FROM public.master_data_import_command_outcomes AS outcome
                    WHERE outcome.school_id = NEW.school_id
                      AND outcome.idempotency_record_id = NEW.id
                LOOP
                    PERFORM app_private.verify_master_data_import_terminal_consistency(
                        NEW.school_id,
                        v_batch_id
                    );
                END LOOP;
                IF TG_OP = 'UPDATE'
                   AND (OLD.school_id, OLD.id) IS DISTINCT FROM (NEW.school_id, NEW.id) THEN
                    FOR v_batch_id IN
                        SELECT outcome.batch_id
                        FROM public.master_data_import_command_outcomes AS outcome
                        WHERE outcome.school_id = OLD.school_id
                          AND outcome.idempotency_record_id = OLD.id
                    LOOP
                        PERFORM app_private.verify_master_data_import_terminal_consistency(
                            OLD.school_id,
                            v_batch_id
                        );
                    END LOOP;
                END IF;
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "app_private.enforce_master_data_import_terminal_consistency() FROM PUBLIC"
    )
    for table_name in (
        "master_data_import_batches",
        "master_data_import_units",
        "master_data_import_command_outcomes",
        "command_idempotency_records",
    ):
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER verify_{table_name}_terminal_consistency
            AFTER INSERT OR UPDATE ON public.{table_name}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION app_private.enforce_master_data_import_terminal_consistency()
            """
        )


def _create_import_audit_boundary() -> None:
    """创建只接受人工任职上下文和固定导入事件的 SECURITY DEFINER 写口。"""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "CREATE POLICY audit_definer_tenant_select "
        "ON public.command_idempotency_records FOR SELECT "
        "TO campus_ai_audit_definer "
        "USING (school_id = (SELECT app.require_school_context()))"
    )
    fact_select_columns = {
        "staff_assignments": (
            "school_id",
            "id",
            "person_id",
            "semantic_role_code",
            "valid_from",
            "valid_to",
            "status",
            "version",
        ),
        "master_data_import_batches": (
            "school_id",
            "id",
            "mode",
            "source_kind",
            "source_ref",
            "status",
            "active_idempotency_record_id",
            "active_action_code",
            "version",
        ),
        "master_data_import_units": (
            "school_id",
            "id",
            "batch_id",
            "unit_kind",
            "status",
            "target_id",
            "target_version",
            "soft_anomalies",
            "decision_reason",
            "last_attempt_idempotency_record_id",
            "last_attempt_completed",
        ),
        "master_data_import_command_outcomes": (
            "school_id",
            "idempotency_record_id",
            "batch_id",
            "action_code",
            "status",
            "batch_version",
        ),
        "master_data_import_takeovers": (
            "school_id",
            "id",
            "batch_id",
            "previous_idempotency_record_id",
            "new_idempotency_record_id",
            "original_actor_user_id",
            "original_actor_person_id",
            "original_actor_assignment_id",
            "original_actor_assignment_version",
            "new_actor_user_id",
            "new_actor_person_id",
            "new_actor_assignment_id",
            "new_actor_assignment_version",
            "request_id",
            "batch_version_before",
            "batch_version_after",
        ),
        "command_idempotency_records": (
            "school_id",
            "id",
            "action_code",
            "actor_user_id",
            "actor_person_id",
            "actor_assignment_id",
            "actor_assignment_version",
            "resource_type",
            "resource_id",
            "idempotency_key_hash",
            "status",
            "result_type",
            "result_payload",
        ),
        "school_people": ("school_id", "id", "status", "version"),
        "school_calendar_versions": (
            "school_id",
            "id",
            "status",
            "version",
            "source_kind",
            "source_ref",
        ),
    }
    for table_name, columns in fact_select_columns.items():
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE public.{table_name} "
            "FROM campus_ai_audit_definer CASCADE"
        )
        op.execute(
            f"GRANT SELECT ({', '.join(columns)}) ON TABLE public.{table_name} "
            "TO campus_ai_audit_definer"
        )
    for table_name in (
        "master_data_import_batches",
        "master_data_import_units",
        "master_data_import_command_outcomes",
        "master_data_import_takeovers",
        "command_idempotency_records",
        "school_people",
        "school_calendar_versions",
    ):
        op.execute(
            f"GRANT SELECT (xmin) ON TABLE public.{table_name} "
            "TO campus_ai_audit_definer"
        )
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE public.audit_logs FROM campus_ai_audit_definer CASCADE"
    )
    op.execute(
        "GRANT INSERT (actor_user_id, school_id, actor_person_id, actor_assignment_id, "
        "actor_assignment_version, actor_guardian_relationship_id, "
        "actor_guardian_relationship_version, actor_context_type, "
        "idempotency_key_hash, event_type, target_type, target_id, before_summary, "
        "after_summary, request_id, reason) ON TABLE public.audit_logs "
        "TO campus_ai_audit_definer"
    )
    op.execute("GRANT SELECT (id) ON TABLE public.audit_logs TO campus_ai_audit_definer")
    op.execute(
        "REVOKE ALL PRIVILEGES ON SEQUENCE public.audit_logs_id_seq "
        "FROM campus_ai_audit_definer CASCADE"
    )
    op.execute("GRANT USAGE ON SEQUENCE public.audit_logs_id_seq TO campus_ai_audit_definer")
    op.execute("GRANT USAGE, CREATE ON SCHEMA app TO campus_ai_audit_definer")
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    op.execute(
        """
        CREATE FUNCTION app.append_master_data_import_audit(
            p_actor_user_id integer,
            p_school_id uuid,
            p_actor_person_id uuid,
            p_actor_assignment_id uuid,
            p_actor_assignment_version bigint,
            p_idempotency_key_hash text,
            p_event_type text,
            p_target_type text,
            p_target_id uuid,
            p_batch_id uuid,
            p_unit_id uuid,
            p_before_summary jsonb,
            p_after_summary jsonb,
            p_request_id uuid,
            p_reason_code text
        )
        RETURNS bigint
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_audit_id bigint;
            v_actor_user_id integer := p_actor_user_id;
            v_school_id uuid := p_school_id;
            v_actor_person_id uuid := p_actor_person_id;
            v_actor_assignment_id uuid := p_actor_assignment_id;
            v_actor_assignment_version bigint := p_actor_assignment_version;
            v_idempotency_key_hash text := p_idempotency_key_hash;
            v_request_id uuid := p_request_id;
            v_now timestamp with time zone := pg_catalog.clock_timestamp();
            v_authorized boolean;
            v_allowed_event boolean;
            v_safe_summaries boolean;
            v_fact_matches boolean := false;
            -- PostgreSQL exposes xmin as the low 32 bits of the assigning XID.
            v_current_xid bigint := (
                pg_current_xact_id()::text::bigint % 4294967296
            );
        BEGIN
            v_school_id := app.require_school_context();
            IF current_setting('app.context_purpose', true) IS DISTINCT FROM 'command'
               OR current_setting('app.context_kind', true) IS DISTINCT FROM 'assignment'
               OR current_setting('app.guardian_relationship_id', true) IS DISTINCT FROM '-' THEN
                RAISE EXCEPTION 'master data import audit requires staff command context'
                    USING ERRCODE = '28000';
            END IF;
            v_actor_user_id := current_setting('app.account_user_id', true)::integer;
            v_actor_person_id := current_setting('app.person_id', true)::uuid;
            v_actor_assignment_id := current_setting('app.assignment_id', true)::uuid;
            v_actor_assignment_version := current_setting(
                'app.assignment_version', true
            )::bigint;
            v_idempotency_key_hash := current_setting('app.idempotency_key_hash', true);
            v_request_id := current_setting('app.request_id', true)::uuid;
            IF p_actor_user_id IS DISTINCT FROM v_actor_user_id
               OR p_school_id IS DISTINCT FROM v_school_id
               OR p_actor_person_id IS DISTINCT FROM v_actor_person_id
               OR p_actor_assignment_id IS DISTINCT FROM v_actor_assignment_id
               OR p_actor_assignment_version IS DISTINCT FROM v_actor_assignment_version
               OR p_idempotency_key_hash IS DISTINCT FROM v_idempotency_key_hash
               OR p_request_id IS DISTINCT FROM v_request_id THEN
                RAISE EXCEPTION 'master data import audit context mismatch'
                    USING ERRCODE = '28000';
            END IF;

            SELECT EXISTS (
                SELECT 1
                FROM public.staff_assignments AS assignment
                WHERE assignment.school_id = v_school_id
                  AND assignment.id = v_actor_assignment_id
                  AND assignment.person_id = v_actor_person_id
                  AND assignment.version = v_actor_assignment_version
                  AND assignment.status = 'confirmed'
                  AND assignment.semantic_role_code = 'school_master_data_manager'
                  AND assignment.valid_from <= v_now
                  AND (assignment.valid_to IS NULL OR assignment.valid_to > v_now)
            ) INTO v_authorized;
            IF v_authorized IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'master data import audit assignment is not authorized'
                    USING ERRCODE = '28000';
            END IF;

            v_allowed_event := (
                p_event_type = 'master_data.person.imported'
                AND p_target_type = 'school_person'
                AND p_reason_code IN (
                    'manual.person-import-confirmed',
                    'excel.person-import-confirmed'
                )
            ) OR (
                p_event_type = 'master_data.calendar.published'
                AND p_target_type = 'school_calendar_version'
                AND p_reason_code = 'excel.calendar-published'
            ) OR (
                p_event_type = 'master_data.import.taken_over'
                AND p_target_type = 'master_data_import_takeover'
                AND p_reason_code = 'manual.ordinary-import-taken-over'
            );
            v_safe_summaries :=
                jsonb_typeof(p_before_summary) = 'object'
                AND jsonb_typeof(p_after_summary) = 'object'
                AND p_before_summary ?& ARRAY[
                    'state_code', 'resource_version', 'stable_reference_ids', 'fact_codes'
                ]
                AND p_after_summary ?& ARRAY[
                    'state_code', 'resource_version', 'stable_reference_ids', 'fact_codes'
                ]
                AND (SELECT count(*) FROM jsonb_object_keys(p_before_summary)) = 4
                AND (SELECT count(*) FROM jsonb_object_keys(p_after_summary)) = 4
                AND jsonb_typeof(p_before_summary -> 'stable_reference_ids') = 'array'
                AND jsonb_typeof(p_after_summary -> 'stable_reference_ids') = 'array'
                AND jsonb_typeof(p_before_summary -> 'fact_codes') = 'array'
                AND jsonb_typeof(p_after_summary -> 'fact_codes') = 'array'
                AND NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(
                        p_before_summary -> 'stable_reference_ids'
                        || p_after_summary -> 'stable_reference_ids'
                    ) AS value(item)
                    WHERE jsonb_typeof(value.item) <> 'string'
                       OR (value.item #>> '{}')
                          !~* '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
                );
            IF v_allowed_event IS DISTINCT FROM TRUE
               OR v_safe_summaries IS DISTINCT FROM TRUE
               OR ((
                (
                    p_event_type = 'master_data.person.imported'
                    AND jsonb_typeof(p_before_summary -> 'state_code') = 'string'
                    AND p_before_summary ->> 'state_code' = 'person.absent'
                    AND jsonb_typeof(p_before_summary -> 'resource_version') = 'null'
                    AND p_before_summary -> 'resource_version' = 'null'::jsonb
                    AND p_before_summary -> 'stable_reference_ids' = '[]'::jsonb
                    AND p_before_summary -> 'fact_codes' = '[]'::jsonb
                    AND jsonb_typeof(p_after_summary -> 'state_code') = 'string'
                    AND p_after_summary ->> 'state_code' = 'person.confirmed'
                    AND jsonb_typeof(p_after_summary -> 'resource_version') = 'number'
                    AND p_after_summary -> 'resource_version' = '1'::jsonb
                    AND p_after_summary -> 'stable_reference_ids'
                        = jsonb_build_array(p_target_id::text)
                    AND p_after_summary -> 'fact_codes' IN (
                        '["person.imported"]'::jsonb,
                        '["person.imported", "person.same_name_manually_continued"]'::jsonb
                    )
                ) OR (
                    p_event_type = 'master_data.calendar.published'
                    AND jsonb_typeof(p_before_summary -> 'state_code') = 'string'
                    AND p_before_summary ->> 'state_code' = 'calendar.draft'
                    AND jsonb_typeof(p_before_summary -> 'resource_version') = 'number'
                    AND p_before_summary -> 'resource_version' = '1'::jsonb
                    AND p_before_summary -> 'stable_reference_ids'
                        = jsonb_build_array(p_target_id::text)
                    AND p_before_summary -> 'fact_codes' = '[]'::jsonb
                    AND jsonb_typeof(p_after_summary -> 'state_code') = 'string'
                    AND p_after_summary ->> 'state_code' = 'calendar.published'
                    AND jsonb_typeof(p_after_summary -> 'resource_version') = 'number'
                    AND p_after_summary -> 'resource_version' = '2'::jsonb
                    AND p_after_summary -> 'stable_reference_ids'
                        = jsonb_build_array(p_target_id::text)
                    AND p_after_summary -> 'fact_codes' = '["calendar.published"]'::jsonb
                ) OR (
                    p_event_type = 'master_data.import.taken_over'
                    AND p_unit_id IS NULL
                    AND p_before_summary = jsonb_build_object(
                        'state_code', 'ordinary_import.active_command',
                        'resource_version', NULL,
                        'stable_reference_ids', jsonb_build_array(p_batch_id::text),
                        'fact_codes', '[]'::jsonb
                    )
                    AND p_after_summary = jsonb_build_object(
                        'state_code', 'ordinary_import.taken_over',
                        'resource_version', NULL,
                        'stable_reference_ids', jsonb_build_array(
                            p_batch_id::text, p_target_id::text
                        ),
                        'fact_codes', '["ordinary_import.taken_over"]'::jsonb
                    )
                )
            )) IS DISTINCT FROM TRUE THEN
                RAISE EXCEPTION 'unsafe master data import audit record'
                    USING ERRCODE = '22023';
            END IF;

            IF p_event_type = 'master_data.person.imported' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM public.master_data_import_units AS unit
                    JOIN public.master_data_import_batches AS batch
                      ON batch.school_id = unit.school_id
                     AND batch.id = unit.batch_id
                    JOIN public.school_people AS person
                      ON person.school_id = unit.school_id
                     AND person.id = unit.target_id
                    WHERE unit.school_id = v_school_id
                      AND unit.id = p_unit_id
                      AND unit.batch_id = p_batch_id
                      AND unit.unit_kind = 'person'
                      AND unit.status = 'applied'
                      AND unit.target_id = p_target_id
                      AND unit.target_version = 1
                      AND unit.xmin::text::bigint = v_current_xid
                      AND person.status = 'active'
                      AND person.version = 1
                      AND person.xmin::text::bigint = v_current_xid
                      AND batch.xmin::text::bigint = v_current_xid
                      AND (
                        (
                            p_after_summary -> 'fact_codes' =
                                '["person.imported", "person.same_name_manually_continued"]'::jsonb
                            AND unit.soft_anomalies @>
                                '[{"code": "same_display_name"}]'::jsonb
                            AND NULLIF(btrim(unit.decision_reason), '') IS NOT NULL
                        ) OR (
                            p_after_summary -> 'fact_codes' = '["person.imported"]'::jsonb
                            AND NOT unit.soft_anomalies @>
                                '[{"code": "same_display_name"}]'::jsonb
                            AND unit.decision_reason IS NULL
                        )
                      )
                      AND (
                        (
                            batch.mode = 'manual_unit'
                            AND batch.source_kind = 'manual'
                            AND batch.status = 'applied'
                            AND p_reason_code = 'manual.person-import-confirmed'
                            AND EXISTS (
                                SELECT 1
                                FROM public.master_data_import_command_outcomes AS outcome
                                JOIN public.command_idempotency_records AS record
                                  ON record.school_id = outcome.school_id
                                 AND record.id = outcome.idempotency_record_id
                                WHERE outcome.school_id = v_school_id
                                  AND outcome.batch_id = batch.id
                                  AND outcome.action_code =
                                      'master_data.import.manual_confirm'
                                  AND outcome.status = 'applied'
                                  AND outcome.batch_version = batch.version
                                  AND outcome.xmin::text::bigint = v_current_xid
                                  AND record.action_code = outcome.action_code
                                  AND record.actor_user_id = v_actor_user_id
                                  AND record.actor_person_id = v_actor_person_id
                                  AND record.actor_assignment_id = v_actor_assignment_id
                                  AND record.actor_assignment_version =
                                      v_actor_assignment_version
                                  AND record.idempotency_key_hash =
                                      v_idempotency_key_hash
                                  AND record.status = 'succeeded'
                                  AND record.result_type =
                                      'master_data.import_outcome'
                                  AND record.result_payload ->> 'resource_type' =
                                      'master_data.import_batch'
                                  AND record.result_payload ->> 'resource_id' =
                                      batch.id::text
                                  AND record.result_payload ->> 'resource_version' =
                                      batch.version::text
                                  AND record.result_payload ->> 'outcome_code' = 'applied'
                                  AND record.xmin::text::bigint = v_current_xid
                            )
                        ) OR (
                            batch.mode = 'ordinary_atomic_units'
                            AND batch.source_kind = 'excel'
                            AND batch.status = 'applying'
                            AND p_reason_code = 'excel.person-import-confirmed'
                            AND unit.last_attempt_completed IS TRUE
                            AND (
                                EXISTS (
                                    SELECT 1
                                    FROM public.command_idempotency_records AS record
                                    WHERE record.school_id = v_school_id
                                      AND record.id =
                                          unit.last_attempt_idempotency_record_id
                                      AND record.id =
                                          batch.active_idempotency_record_id
                                      AND record.action_code = batch.active_action_code
                                      AND record.actor_user_id = v_actor_user_id
                                      AND record.actor_person_id = v_actor_person_id
                                      AND record.actor_assignment_id =
                                          v_actor_assignment_id
                                      AND record.actor_assignment_version =
                                          v_actor_assignment_version
                                      AND record.idempotency_key_hash =
                                          v_idempotency_key_hash
                                      AND record.status = 'in_progress'
                                ) OR EXISTS (
                                    SELECT 1
                                    FROM public.master_data_import_takeovers AS takeover
                                    JOIN public.command_idempotency_records AS previous_record
                                      ON previous_record.school_id = takeover.school_id
                                     AND previous_record.id =
                                         takeover.previous_idempotency_record_id
                                    JOIN public.command_idempotency_records AS new_record
                                      ON new_record.school_id = takeover.school_id
                                     AND new_record.id =
                                         takeover.new_idempotency_record_id
                                    WHERE takeover.school_id = v_school_id
                                      AND takeover.batch_id = batch.id
                                      AND takeover.previous_idempotency_record_id =
                                          unit.last_attempt_idempotency_record_id
                                      AND takeover.new_idempotency_record_id =
                                          batch.active_idempotency_record_id
                                      AND takeover.original_actor_user_id =
                                          previous_record.actor_user_id
                                      AND takeover.original_actor_person_id =
                                          previous_record.actor_person_id
                                      AND takeover.original_actor_assignment_id =
                                          previous_record.actor_assignment_id
                                      AND takeover.original_actor_assignment_version =
                                          previous_record.actor_assignment_version
                                      AND takeover.new_actor_user_id = v_actor_user_id
                                      AND takeover.new_actor_person_id = v_actor_person_id
                                      AND takeover.new_actor_assignment_id =
                                          v_actor_assignment_id
                                      AND takeover.new_actor_assignment_version =
                                          v_actor_assignment_version
                                      AND takeover.request_id = v_request_id
                                      AND takeover.batch_version_before + 1 =
                                          takeover.batch_version_after
                                      AND takeover.batch_version_after <= batch.version
                                      AND previous_record.action_code IN (
                                          'master_data.import.ordinary_confirm',
                                          'master_data.import.ordinary_resume'
                                      )
                                      AND previous_record.resource_type =
                                          'master_data.import_batch'
                                      AND previous_record.resource_id = batch.id::text
                                      AND previous_record.status = 'terminal_failure'
                                      AND previous_record.result_type =
                                          'master_data.import_outcome'
                                      AND previous_record.result_payload ->> 'resource_type' =
                                          'master_data.import_batch'
                                      AND previous_record.result_payload ->> 'resource_id' =
                                          batch.id::text
                                      AND previous_record.result_payload ->> 'resource_version' =
                                          takeover.batch_version_before::text
                                      AND previous_record.result_payload ->> 'outcome_code' =
                                          'master_data.import.command_superseded'
                                      AND new_record.actor_user_id = v_actor_user_id
                                      AND new_record.actor_person_id = v_actor_person_id
                                      AND new_record.actor_assignment_id =
                                          v_actor_assignment_id
                                      AND new_record.actor_assignment_version =
                                          v_actor_assignment_version
                                      AND new_record.idempotency_key_hash =
                                          v_idempotency_key_hash
                                      AND new_record.status = 'in_progress'
                                      AND new_record.action_code =
                                          'master_data.import.ordinary_takeover'
                                      AND new_record.resource_type =
                                          'master_data.import_batch'
                                      AND new_record.resource_id = batch.id::text
                                )
                            )
                        )
                      )
                ) INTO v_fact_matches;
            ELSIF p_event_type = 'master_data.calendar.published' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM public.master_data_import_units AS unit
                    JOIN public.master_data_import_batches AS batch
                      ON batch.school_id = unit.school_id
                     AND batch.id = unit.batch_id
                    JOIN public.school_calendar_versions AS calendar
                      ON calendar.school_id = unit.school_id
                     AND calendar.id = unit.target_id
                    JOIN public.master_data_import_command_outcomes AS outcome
                      ON outcome.school_id = batch.school_id
                     AND outcome.batch_id = batch.id
                    JOIN public.command_idempotency_records AS record
                      ON record.school_id = outcome.school_id
                     AND record.id = outcome.idempotency_record_id
                    WHERE unit.school_id = v_school_id
                      AND unit.id = p_unit_id
                      AND unit.batch_id = p_batch_id
                      AND unit.unit_kind = 'calendar_set'
                      AND unit.status = 'published'
                      AND unit.target_id = p_target_id
                      AND unit.target_version = 2
                      AND unit.xmin::text::bigint = v_current_xid
                      AND batch.mode = 'authoritative_set_publish'
                      AND batch.source_kind = 'excel'
                      AND batch.status = 'published'
                      AND batch.xmin::text::bigint = v_current_xid
                      AND calendar.status = 'published'
                      AND calendar.version = 2
                      AND calendar.source_kind = 'master_data_import'
                      AND calendar.source_ref = batch.source_ref
                      AND calendar.xmin::text::bigint = v_current_xid
                      AND outcome.action_code = 'master_data.import.calendar_publish'
                      AND outcome.status = 'published'
                      AND outcome.batch_version = batch.version
                      AND outcome.xmin::text::bigint = v_current_xid
                      AND record.action_code = outcome.action_code
                      AND record.actor_user_id = v_actor_user_id
                      AND record.actor_person_id = v_actor_person_id
                      AND record.actor_assignment_id = v_actor_assignment_id
                      AND record.actor_assignment_version = v_actor_assignment_version
                      AND record.idempotency_key_hash = v_idempotency_key_hash
                      AND record.status = 'succeeded'
                      AND record.result_type = 'master_data.import_outcome'
                      AND record.result_payload ->> 'resource_type' =
                          'master_data.import_batch'
                      AND record.result_payload ->> 'resource_id' = batch.id::text
                      AND record.result_payload ->> 'resource_version' =
                          batch.version::text
                      AND record.result_payload ->> 'outcome_code' = 'published'
                      AND record.xmin::text::bigint = v_current_xid
                ) INTO v_fact_matches;
            ELSE
                SELECT EXISTS (
                    SELECT 1
                    FROM public.master_data_import_takeovers AS takeover
                    JOIN public.master_data_import_batches AS batch
                      ON batch.school_id = takeover.school_id
                     AND batch.id = takeover.batch_id
                    JOIN public.command_idempotency_records AS previous_record
                      ON previous_record.school_id = takeover.school_id
                     AND previous_record.id = takeover.previous_idempotency_record_id
                    JOIN public.command_idempotency_records AS new_record
                      ON new_record.school_id = takeover.school_id
                     AND new_record.id = takeover.new_idempotency_record_id
                    WHERE takeover.school_id = v_school_id
                      AND takeover.id = p_target_id
                      AND takeover.batch_id = p_batch_id
                      AND p_unit_id IS NULL
                      AND takeover.xmin::text::bigint = v_current_xid
                      AND takeover.original_actor_user_id =
                          previous_record.actor_user_id
                      AND takeover.original_actor_person_id =
                          previous_record.actor_person_id
                      AND takeover.original_actor_assignment_id =
                          previous_record.actor_assignment_id
                      AND takeover.original_actor_assignment_version =
                          previous_record.actor_assignment_version
                      AND takeover.new_actor_user_id = v_actor_user_id
                      AND takeover.new_actor_person_id = v_actor_person_id
                      AND takeover.new_actor_assignment_id = v_actor_assignment_id
                      AND takeover.new_actor_assignment_version =
                          v_actor_assignment_version
                      AND takeover.request_id = v_request_id
                      AND takeover.batch_version_before + 1 =
                          takeover.batch_version_after
                      AND batch.mode = 'ordinary_atomic_units'
                      AND batch.source_kind = 'excel'
                      AND batch.status = 'applying'
                      AND batch.version = takeover.batch_version_after
                      AND batch.xmin::text::bigint = v_current_xid
                      AND batch.active_idempotency_record_id = new_record.id
                      AND batch.active_action_code =
                          'master_data.import.ordinary_takeover'
                      AND previous_record.status = 'terminal_failure'
                      AND previous_record.action_code IN (
                          'master_data.import.ordinary_confirm',
                          'master_data.import.ordinary_resume'
                      )
                      AND previous_record.resource_type = 'master_data.import_batch'
                      AND previous_record.resource_id = batch.id::text
                      AND previous_record.result_type =
                          'master_data.import_outcome'
                      AND previous_record.result_payload ->> 'resource_type' =
                          'master_data.import_batch'
                      AND previous_record.result_payload ->> 'resource_id' = batch.id::text
                      AND previous_record.result_payload ->> 'resource_version' =
                          takeover.batch_version_before::text
                      AND previous_record.result_payload ->> 'outcome_code' =
                          'master_data.import.command_superseded'
                      AND previous_record.xmin::text::bigint = v_current_xid
                      AND new_record.action_code =
                          'master_data.import.ordinary_takeover'
                      AND new_record.resource_type = 'master_data.import_batch'
                      AND new_record.resource_id = batch.id::text
                      AND new_record.actor_user_id = v_actor_user_id
                      AND new_record.actor_person_id = v_actor_person_id
                      AND new_record.actor_assignment_id = v_actor_assignment_id
                      AND new_record.actor_assignment_version =
                          v_actor_assignment_version
                      AND new_record.idempotency_key_hash = v_idempotency_key_hash
                      AND new_record.status = 'in_progress'
                      AND new_record.xmin::text::bigint = v_current_xid
                ) INTO v_fact_matches;
            END IF;
            IF v_fact_matches IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'master data import audit fact mismatch'
                    USING ERRCODE = '23514';
            END IF;

            INSERT INTO public.audit_logs (
                actor_user_id, school_id, actor_person_id, actor_assignment_id,
                actor_assignment_version, actor_context_type,
                idempotency_key_hash, event_type, target_type, target_id,
                before_summary, after_summary, request_id, reason
            ) VALUES (
                v_actor_user_id, v_school_id, v_actor_person_id, v_actor_assignment_id,
                v_actor_assignment_version, 'assignment',
                v_idempotency_key_hash, p_event_type, p_target_type, p_target_id::text,
                p_before_summary, p_after_summary, v_request_id::text, p_reason_code
            )
            RETURNING id INTO v_audit_id;
            RETURN v_audit_id;
        END
        $function$
        """
    )
    signature = (
        "app.append_master_data_import_audit(integer,uuid,uuid,uuid,bigint,text,text,"
        "text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_runtime")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_audit_definer")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")


def upgrade() -> None:
    """创建首批导入草稿/单元表并从同一 revision 强制租户 RLS。"""

    _create_import_tables()
    _enable_rls_and_grant_runtime()
    _create_import_takeover_immutability_triggers()
    _create_import_unit_immutability_trigger()
    _create_import_batch_guard()
    _create_terminal_consistency_triggers()
    op.create_index(
        "uq_school_person_names_external_reference",
        "school_person_names",
        ["school_id", "source_kind", "source_ref"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'confirmed' AND source_kind = 'person_external_reference'"
        ),
    )
    _create_import_audit_boundary()
    op.create_index(
        "uq_audit_logs_master_data_import_fact",
        "audit_logs",
        [
            "school_id",
            "idempotency_key_hash",
            "event_type",
            "target_type",
            "target_id",
        ],
        unique=True,
        postgresql_where=sa.text(
            "event_type IN ('master_data.person.imported', "
            "'master_data.calendar.published', 'master_data.import.taken_over')"
        ),
    )


def downgrade() -> None:
    """撤销 Task 8 表授权并回到不含导入草稿的 08 结构。"""

    signature = (
        "app.append_master_data_import_audit(integer,uuid,uuid,uuid,bigint,text,text,"
        "text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)"
    )
    legacy_signature = (
        "app.append_master_data_import_audit(integer,uuid,uuid,uuid,bigint,text,text,"
        "text,uuid,jsonb,jsonb,uuid,text)"
    )
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute(f"DROP FUNCTION IF EXISTS {legacy_signature}")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "DROP POLICY IF EXISTS audit_definer_tenant_select ON public.command_idempotency_records"
    )
    for table_name in (
        "staff_assignments",
        "master_data_import_batches",
        "master_data_import_units",
        "master_data_import_command_outcomes",
        "master_data_import_takeovers",
        "command_idempotency_records",
        "school_people",
        "school_calendar_versions",
    ):
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE public.{table_name} "
            "FROM campus_ai_audit_definer CASCADE"
        )
    op.execute("DROP INDEX IF EXISTS public.uq_audit_logs_master_data_import_fact")
    op.drop_index(
        "uq_school_person_names_external_reference",
        table_name="school_person_names",
    )
    for table_name in (
        "master_data_import_batches",
        "master_data_import_units",
        "master_data_import_command_outcomes",
        "command_idempotency_records",
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS verify_{table_name}_terminal_consistency "
            f"ON public.{table_name}"
        )
    op.execute(
        "DROP FUNCTION IF EXISTS app_private.enforce_master_data_import_terminal_consistency()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "app_private.verify_master_data_import_terminal_consistency(uuid,uuid)"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS enforce_master_data_import_batch_guard "
        "ON public.master_data_import_batches"
    )
    op.execute("DROP FUNCTION IF EXISTS app_private.enforce_master_data_import_batch_guard()")
    op.execute(
        "DROP TRIGGER IF EXISTS enforce_master_data_import_unit_immutability "
        "ON public.master_data_import_units"
    )
    op.execute("DROP FUNCTION IF EXISTS app_private.enforce_master_data_import_unit_immutability()")
    op.execute(
        "DROP TRIGGER IF EXISTS reject_master_data_import_takeover_truncate "
        "ON public.master_data_import_takeovers"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS reject_master_data_import_takeover_row_mutation "
        "ON public.master_data_import_takeovers"
    )
    op.execute("DROP FUNCTION IF EXISTS app_private.reject_master_data_import_takeover_mutation()")
    for table_name in reversed(_IMPORT_TABLES):
        op.execute(f"REVOKE ALL ON TABLE public.{table_name} FROM campus_ai_runtime")
        op.drop_table(table_name)
    op.drop_constraint(
        "uq_command_idempotency_records_school_id_id",
        "command_idempotency_records",
        type_="unique",
    )
