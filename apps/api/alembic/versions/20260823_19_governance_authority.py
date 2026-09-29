"""Create the Governance authorization authority.

Revision ID: 20260823_19
Revises: 20260821_18
Create Date: 2026-08-23
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260823_19"
down_revision = "20260821_18"
branch_labels = None
depends_on = None

_TABLES = (
    "governance_duties",
    "governance_duty_versions",
    "governance_scopes",
    "governance_scope_versions",
    "governance_grants",
    "governance_grant_versions",
    "governance_assignment_epochs",
    "governance_bootstrap_states",
    "governance_command_receipts",
    "governance_audit_events",
)
_RUNTIME_READ_TABLES = (
    "governance_duties",
    "governance_duty_versions",
    "governance_scopes",
    "governance_scope_versions",
    "governance_grants",
    "governance_grant_versions",
    "governance_assignment_epochs",
)


def _timestamps() -> tuple[sa.Column[Any], sa.Column[Any]]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def _create_duties() -> None:
    op.create_table(
        "governance_duties",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("duty_code", sa.String(length=120), nullable=False),
        sa.Column("current_version", sa.BigInteger(), server_default="1", nullable=False),
        *_timestamps(),
        sa.CheckConstraint("btrim(duty_code) <> ''", name="ck_governance_duties_code_nonempty"),
        sa.CheckConstraint("current_version >= 1", name="ck_governance_duties_version_positive"),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("school_id", "duty_code", name="uq_governance_duties_school_duty_code"),
        sa.UniqueConstraint("school_id", "id", name="uq_governance_duties_school_id_id"),
    )
    op.create_table(
        "governance_duty_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("duty_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "btrim(display_name) <> ''",
            name="ck_governance_duty_versions_name_nonempty",
        ),
        sa.CheckConstraint("version >= 1", name="ck_governance_duty_versions_version_positive"),
        sa.ForeignKeyConstraint(
            ["school_id", "duty_id"],
            ["governance_duties.school_id", "governance_duties.id"],
            name="fk_governance_duty_versions_duty",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "school_id",
            "duty_id",
            "version",
            name="uq_governance_duty_versions_target_version",
        ),
    )
    op.create_index(
        "ix_governance_duty_versions_as_of",
        "governance_duty_versions",
        ["school_id", "duty_id", "effective_from", "version"],
    )


def _create_scopes() -> None:
    op.create_table(
        "governance_scopes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("scope_key", sa.String(length=160), nullable=False),
        sa.Column("current_version", sa.BigInteger(), server_default="1", nullable=False),
        *_timestamps(),
        sa.CheckConstraint("btrim(scope_key) <> ''", name="ck_governance_scopes_key_nonempty"),
        sa.CheckConstraint("current_version >= 1", name="ck_governance_scopes_version_positive"),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("school_id", "scope_key", name="uq_governance_scopes_school_scope_key"),
        sa.UniqueConstraint("school_id", "id", name="uq_governance_scopes_school_id_id"),
    )
    op.create_table(
        "governance_scope_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("schema_owner", sa.String(length=120), nullable=False),
        sa.Column("schema_name", sa.String(length=120), nullable=False),
        sa.Column("schema_version", sa.BigInteger(), nullable=False),
        sa.Column("reference_document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reference_sha256", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reference_document) = 'object'",
            name="ck_governance_scope_versions_document_object",
        ),
        sa.CheckConstraint(
            "btrim(schema_owner) <> ''",
            name="ck_governance_scope_versions_owner_nonempty",
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name="ck_governance_scope_versions_schema_version_positive",
        ),
        sa.CheckConstraint(
            "btrim(schema_name) <> ''",
            name="ck_governance_scope_versions_schema_nonempty",
        ),
        sa.CheckConstraint(
            "reference_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_governance_scope_versions_sha256",
        ),
        sa.CheckConstraint("version >= 1", name="ck_governance_scope_versions_version_positive"),
        sa.ForeignKeyConstraint(
            ["school_id", "scope_id"],
            ["governance_scopes.school_id", "governance_scopes.id"],
            name="fk_governance_scope_versions_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "school_id",
            "scope_id",
            "version",
            name="uq_governance_scope_versions_target_version",
        ),
    )
    op.create_index(
        "ix_governance_scope_versions_as_of",
        "governance_scope_versions",
        ["school_id", "scope_id", "effective_from", "version"],
    )
    op.create_index(
        "ix_governance_scope_versions_schema",
        "governance_scope_versions",
        ["schema_owner", "schema_name", "schema_version"],
    )


def _create_grants() -> None:
    op.create_table(
        "governance_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("duty_id", sa.Uuid(), nullable=False),
        sa.Column("current_version", sa.BigInteger(), server_default="1", nullable=False),
        *_timestamps(),
        sa.CheckConstraint("current_version >= 1", name="ck_governance_grants_version_positive"),
        sa.ForeignKeyConstraint(
            ["school_id", "assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_governance_grants_assignment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "duty_id"],
            ["governance_duties.school_id", "governance_duties.id"],
            name="fk_governance_grants_duty",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "school_id", "id", "duty_id", name="uq_governance_grants_school_id_duty"
        ),
        sa.UniqueConstraint("school_id", "id", name="uq_governance_grants_school_id_id"),
    )
    op.create_index(
        "ix_governance_grants_assignment",
        "governance_grants",
        ["school_id", "assignment_id", "duty_id", "current_version", "id"],
    )
    op.create_table(
        "governance_grant_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("duty_id", sa.Uuid(), nullable=False),
        sa.Column("duty_version", sa.BigInteger(), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("scope_version", sa.BigInteger(), nullable=False),
        sa.Column("basis_authority", sa.String(length=120), nullable=False),
        sa.Column("basis_reference", sa.String(length=255), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("formal_ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transition_kind", sa.String(length=20), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "btrim(basis_authority) <> ''",
            name="ck_governance_grant_versions_basis_authority",
        ),
        sa.CheckConstraint(
            "btrim(basis_reference) <> ''",
            name="ck_governance_grant_versions_basis_reference",
        ),
        sa.CheckConstraint(
            "(transition_kind = 'create' AND version = 1) OR "
            "(transition_kind <> 'create' AND version > 1)",
            name="ck_governance_grant_versions_create_version",
        ),
        sa.CheckConstraint(
            "effective_from >= valid_from",
            name="ck_governance_grant_versions_effective_order",
        ),
        sa.CheckConstraint(
            "planned_valid_to IS NULL OR planned_valid_to > valid_from",
            name="ck_governance_grant_versions_planned_interval",
        ),
        sa.CheckConstraint(
            "duty_version >= 1 AND scope_version >= 1 AND version >= 1",
            name="ck_governance_grant_versions_positive_versions",
        ),
        sa.CheckConstraint(
            "(transition_kind IN ('create', 'update') "
            "AND formal_ended_at IS NULL AND revoked_at IS NULL) OR "
            "(transition_kind = 'end' AND formal_ended_at = effective_from "
            "AND revoked_at IS NULL) OR "
            "(transition_kind = 'revoke' AND revoked_at = effective_from "
            "AND formal_ended_at IS NULL)",
            name="ck_governance_grant_versions_terminal_shape",
        ),
        sa.CheckConstraint(
            "transition_kind IN ('create', 'update', 'end', 'revoke')",
            name="ck_governance_grant_versions_transition",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "duty_id", "duty_version"],
            [
                "governance_duty_versions.school_id",
                "governance_duty_versions.duty_id",
                "governance_duty_versions.version",
            ],
            name="fk_governance_grant_versions_duty_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "grant_id", "duty_id"],
            [
                "governance_grants.school_id",
                "governance_grants.id",
                "governance_grants.duty_id",
            ],
            name="fk_governance_grant_versions_grant_duty",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "scope_id", "scope_version"],
            [
                "governance_scope_versions.school_id",
                "governance_scope_versions.scope_id",
                "governance_scope_versions.version",
            ],
            name="fk_governance_grant_versions_scope_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "school_id",
            "grant_id",
            "version",
            name="uq_governance_grant_versions_target_version",
        ),
    )
    op.create_index(
        "ix_governance_grant_versions_as_of",
        "governance_grant_versions",
        ["school_id", "grant_id", "effective_from", "version"],
    )
    op.create_index(
        "ix_governance_grant_versions_scope",
        "governance_grant_versions",
        ["school_id", "scope_id", "scope_version"],
    )


def _create_support_tables() -> None:
    op.create_table(
        "governance_assignment_epochs",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("epoch >= 1", name="ck_governance_assignment_epochs_positive"),
        sa.ForeignKeyConstraint(
            ["school_id", "assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_governance_assignment_epochs_assignment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("school_id", "assignment_id"),
    )
    op.create_table(
        "governance_bootstrap_states",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("authority_reference", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "btrim(authority_reference) <> ''",
            name="ck_governance_bootstrap_states_authority_reference",
        ),
        sa.CheckConstraint(
            "(state = 'open' AND closed_at IS NULL) OR "
            "(state = 'closed' AND closed_at IS NOT NULL)",
            name="ck_governance_bootstrap_states_closed_shape",
        ),
        sa.CheckConstraint(
            "closed_at IS NULL OR closed_at >= opened_at",
            name="ck_governance_bootstrap_states_closed_interval",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'closed')", name="ck_governance_bootstrap_states_state"
        ),
        sa.CheckConstraint("version >= 1", name="ck_governance_bootstrap_states_version_positive"),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("school_id"),
    )


def _create_command_and_audit_tables() -> None:
    op.create_table(
        "governance_command_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("command_name", sa.String(length=120), nullable=False),
        sa.Column("actor_kind", sa.String(length=80), nullable=False),
        sa.Column("actor_reference", sa.String(length=255), nullable=False),
        sa.Column("actor_assignment_id", sa.Uuid(), nullable=True),
        sa.Column("actor_assignment_version", sa.BigInteger(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("target_kind", sa.String(length=80), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("target_version", sa.BigInteger(), nullable=True),
        sa.Column(
            "result_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "(actor_assignment_id IS NULL AND actor_assignment_version IS NULL) OR "
            "(actor_assignment_id IS NOT NULL AND actor_assignment_version >= 1)",
            name="ck_governance_command_receipts_actor_assignment",
        ),
        sa.CheckConstraint(
            "btrim(actor_kind) <> ''", name="ck_governance_command_receipts_actor_kind"
        ),
        sa.CheckConstraint(
            "btrim(actor_reference) <> ''",
            name="ck_governance_command_receipts_actor_reference",
        ),
        sa.CheckConstraint(
            "btrim(command_name) <> ''", name="ck_governance_command_receipts_command"
        ),
        sa.CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$'",
            name="ck_governance_command_receipts_key_hash",
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_governance_command_receipts_request_hash",
        ),
        sa.CheckConstraint(
            "status IN ('started', 'succeeded')",
            name="ck_governance_command_receipts_status",
        ),
        sa.CheckConstraint(
            "(target_id IS NULL AND target_version IS NULL) OR "
            "(target_id IS NOT NULL AND target_version >= 1)",
            name="ck_governance_command_receipts_target",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "actor_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_governance_command_receipts_actor_assignment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("school_id", "id", name="uq_governance_command_receipts_school_id_id"),
        sa.UniqueConstraint(
            "school_id",
            "idempotency_key_hash",
            name="uq_governance_command_receipts_school_key",
        ),
    )
    op.create_index(
        "ix_governance_command_receipts_request",
        "governance_command_receipts",
        ["school_id", "request_id"],
    )
    op.create_table(
        "governance_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("command_receipt_id", sa.Uuid(), nullable=False),
        sa.Column("actor_kind", sa.String(length=80), nullable=False),
        sa.Column("actor_reference", sa.String(length=255), nullable=False),
        sa.Column("actor_assignment_id", sa.Uuid(), nullable=True),
        sa.Column("actor_assignment_version", sa.BigInteger(), nullable=True),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("target_kind", sa.String(length=80), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("target_version", sa.BigInteger(), nullable=False),
        sa.Column("before_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "(actor_assignment_id IS NULL AND actor_assignment_version IS NULL) OR "
            "(actor_assignment_id IS NOT NULL AND actor_assignment_version >= 1)",
            name="ck_governance_audit_events_actor_assignment",
        ),
        sa.CheckConstraint("btrim(actor_kind) <> ''", name="ck_governance_audit_events_actor_kind"),
        sa.CheckConstraint(
            "btrim(actor_reference) <> ''",
            name="ck_governance_audit_events_actor_reference",
        ),
        sa.CheckConstraint("btrim(event_type) <> ''", name="ck_governance_audit_events_event_type"),
        sa.CheckConstraint("btrim(reason) <> ''", name="ck_governance_audit_events_reason"),
        sa.CheckConstraint(
            "btrim(target_kind) <> ''", name="ck_governance_audit_events_target_kind"
        ),
        sa.CheckConstraint("target_version >= 1", name="ck_governance_audit_events_target_version"),
        sa.ForeignKeyConstraint(
            ["school_id", "actor_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_governance_audit_events_actor_assignment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "command_receipt_id"],
            ["governance_command_receipts.school_id", "governance_command_receipts.id"],
            name="fk_governance_audit_events_command",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "school_id",
            "command_receipt_id",
            name="uq_governance_audit_events_command",
        ),
    )
    op.create_index(
        "ix_governance_audit_events_request",
        "governance_audit_events",
        ["school_id", "request_id"],
    )
    op.create_index(
        "ix_governance_audit_events_target",
        "governance_audit_events",
        ["school_id", "target_kind", "target_id", "occurred_at"],
    )


def _close_direct_dml() -> None:
    for table_name in _TABLES:
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE public.{table_name} "
            "FROM PUBLIC, campus_ai_runtime CASCADE"
        )
    for table_name in _RUNTIME_READ_TABLES:
        op.execute(f"GRANT SELECT ON TABLE public.{table_name} TO campus_ai_runtime")


def _create_invariant_guards() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION public.guard_governance_grant_binding()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp
            AS $governance_grant_binding_guard$
            BEGIN
                IF NEW.assignment_id IS DISTINCT FROM OLD.assignment_id
                   OR NEW.duty_id IS DISTINCT FROM OLD.duty_id THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'Governance Grant assignment and duty bindings are immutable';
                END IF;
                RETURN NEW;
            END
            $governance_grant_binding_guard$;

            CREATE TRIGGER trg_governance_grants_immutable_binding
            BEFORE UPDATE ON public.governance_grants
            FOR EACH ROW
            EXECUTE FUNCTION public.guard_governance_grant_binding();
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION public.reject_governance_append_only_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp
            AS $governance_append_only_guard$
            BEGIN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = format('%s is append-only', TG_TABLE_NAME);
            END
            $governance_append_only_guard$;

            CREATE TRIGGER trg_governance_duty_versions_append_only
            BEFORE UPDATE OR DELETE ON public.governance_duty_versions
            FOR EACH ROW
            EXECUTE FUNCTION public.reject_governance_append_only_mutation();

            CREATE TRIGGER trg_governance_scope_versions_append_only
            BEFORE UPDATE OR DELETE ON public.governance_scope_versions
            FOR EACH ROW
            EXECUTE FUNCTION public.reject_governance_append_only_mutation();

            CREATE TRIGGER trg_governance_grant_versions_append_only
            BEFORE UPDATE OR DELETE ON public.governance_grant_versions
            FOR EACH ROW
            EXECUTE FUNCTION public.reject_governance_append_only_mutation();

            CREATE TRIGGER trg_governance_audit_events_append_only
            BEFORE UPDATE OR DELETE ON public.governance_audit_events
            FOR EACH ROW
            EXECUTE FUNCTION public.reject_governance_append_only_mutation();
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION public.guard_governance_bootstrap_state()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp
            AS $governance_bootstrap_state_guard$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    IF OLD.state = 'closed' THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '55000',
                            MESSAGE = 'closed Governance bootstrap authority cannot be deleted';
                    END IF;
                    RETURN OLD;
                END IF;
                IF NEW.version <> OLD.version + 1 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'Governance bootstrap state version must advance by one';
                END IF;
                IF OLD.state = 'closed' AND NEW.state <> 'closed' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'closed Governance bootstrap authority cannot reopen';
                END IF;
                IF OLD.state = 'closed'
                   AND NEW.authority_reference IS DISTINCT FROM OLD.authority_reference THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'closed Governance bootstrap authority cannot change';
                END IF;
                RETURN NEW;
            END
            $governance_bootstrap_state_guard$;

            CREATE TRIGGER trg_governance_bootstrap_states_guard
            BEFORE UPDATE OR DELETE ON public.governance_bootstrap_states
            FOR EACH ROW
            EXECUTE FUNCTION public.guard_governance_bootstrap_state();
            """
        )
    )


def upgrade() -> None:
    """Install the Governance-owned persistence boundary, empty by default."""

    _create_duties()
    _create_scopes()
    _create_grants()
    _create_support_tables()
    _create_command_and_audit_tables()
    _create_invariant_guards()
    _close_direct_dml()


def _guard_empty_downgrade() -> None:
    predicates = " OR ".join(
        f"EXISTS (SELECT 1 FROM public.{table_name} LIMIT 1)" for table_name in _TABLES
    )
    op.execute(
        sa.text(
            f"""
            DO $governance_downgrade_guard$
            BEGIN
                IF {predicates} THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 19 refuses to discard Governance authority data';
                END IF;
            END
            $governance_downgrade_guard$;
            """
        )
    )


def _drop_invariant_guards() -> None:
    triggers = (
        ("governance_grants", "trg_governance_grants_immutable_binding"),
        ("governance_duty_versions", "trg_governance_duty_versions_append_only"),
        ("governance_scope_versions", "trg_governance_scope_versions_append_only"),
        ("governance_grant_versions", "trg_governance_grant_versions_append_only"),
        ("governance_bootstrap_states", "trg_governance_bootstrap_states_guard"),
        ("governance_audit_events", "trg_governance_audit_events_append_only"),
    )
    for table_name, trigger_name in triggers:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON public.{table_name}")
    op.execute("DROP FUNCTION IF EXISTS public.guard_governance_grant_binding()")
    op.execute("DROP FUNCTION IF EXISTS public.reject_governance_append_only_mutation()")
    op.execute("DROP FUNCTION IF EXISTS public.guard_governance_bootstrap_state()")


def downgrade() -> None:
    """Remove only an unused Governance authority schema."""

    _guard_empty_downgrade()
    _drop_invariant_guards()
    for table_name in reversed(_TABLES):
        op.drop_table(table_name)
