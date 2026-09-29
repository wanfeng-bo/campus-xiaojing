"""Add responsibility, idempotency, outbox, and typed audit context.

Revision ID: 20260805_06
Revises: 20260805_05
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260805_06"
down_revision: str | None = "20260805_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONFIRMATION_STATUSES = "status IN ('draft', 'confirmed', 'rejected')"
_VALID_INTERVAL = "valid_to IS NULL OR valid_to > valid_from"


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


def _effective_range() -> sa.TextClause:
    return sa.text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')")


def upgrade() -> None:
    """创建责任和事务记录，并以可空列扩展既有审计历史。"""

    op.create_table(
        "responsibility_types",
        _uuid_column("id"),
        _uuid_column("school_id"),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_responsibility_types"),
        sa.UniqueConstraint("school_id", "id", name="uq_responsibility_types_school_id_id"),
        sa.UniqueConstraint("school_id", "code", name="uq_responsibility_types_school_code"),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_responsibility_types_school",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')", name="ck_responsibility_types_status"
        ),
        sa.CheckConstraint("version >= 1", name="ck_responsibility_types_version_positive"),
    )
    op.create_table(
        "responsibility_groups",
        _uuid_column("id"),
        _uuid_column("school_id"),
        sa.Column("code", sa.String(length=80), nullable=False),
        _uuid_column("manager_assignment_id"),
        sa.Column("manager_assignment_version", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_responsibility_groups"),
        sa.UniqueConstraint("school_id", "id", name="uq_responsibility_groups_school_id_id"),
        sa.UniqueConstraint("school_id", "code", name="uq_responsibility_groups_school_code"),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_responsibility_groups_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "manager_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_responsibility_groups_manager_assignment",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')", name="ck_responsibility_groups_status"
        ),
        sa.CheckConstraint(
            "manager_assignment_version >= 1",
            name="ck_responsibility_groups_manager_assignment_version_positive",
        ),
        sa.CheckConstraint("version >= 1", name="ck_responsibility_groups_version_positive"),
    )
    op.create_table(
        "responsibility_group_versions",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("responsibility_group_id"),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_responsibility_group_versions"),
        sa.ForeignKeyConstraint(
            ["school_id", "responsibility_group_id"],
            ["responsibility_groups.school_id", "responsibility_groups.id"],
            name="fk_responsibility_group_versions_group",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_CONFIRMATION_STATUSES, name="ck_responsibility_group_versions_status"),
        sa.CheckConstraint(_VALID_INTERVAL, name="ck_responsibility_group_versions_valid_interval"),
        sa.CheckConstraint(
            "version >= 1", name="ck_responsibility_group_versions_version_positive"
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("responsibility_group_id", "="),
            (_effective_range(), "&&"),
            where=sa.text("status = 'confirmed'"),
            using="gist",
            name="excl_responsibility_group_versions_confirmed_period",
        ),
    )
    op.create_index(
        "ix_responsibility_group_versions_current",
        "responsibility_group_versions",
        ["school_id", "responsibility_group_id", "status", "valid_from", "valid_to"],
    )
    op.create_table(
        "responsibility_group_memberships",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("responsibility_group_id"),
        _uuid_column("assignment_id"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_responsibility_group_memberships"),
        sa.ForeignKeyConstraint(
            ["school_id", "responsibility_group_id"],
            ["responsibility_groups.school_id", "responsibility_groups.id"],
            name="fk_responsibility_group_memberships_group",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_responsibility_group_memberships_assignment",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _CONFIRMATION_STATUSES, name="ck_responsibility_group_memberships_status"
        ),
        sa.CheckConstraint(
            _VALID_INTERVAL, name="ck_responsibility_group_memberships_valid_interval"
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_responsibility_group_memberships_version_positive"
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("responsibility_group_id", "="),
            ("assignment_id", "="),
            (_effective_range(), "&&"),
            where=sa.text("status = 'confirmed'"),
            using="gist",
            name="excl_responsibility_group_memberships_confirmed_period",
        ),
    )
    op.create_index(
        "ix_responsibility_group_memberships_current_group",
        "responsibility_group_memberships",
        ["school_id", "responsibility_group_id", "status", "valid_from", "valid_to"],
    )
    op.create_index(
        "ix_responsibility_group_memberships_current_assignment",
        "responsibility_group_memberships",
        ["school_id", "assignment_id", "status", "valid_from", "valid_to"],
    )
    op.create_table(
        "repair_responsibility_grants",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("responsibility_type_id"),
        _uuid_column("responsibility_group_id"),
        _uuid_column("location_id", nullable=True),
        sa.Column("repair_subtype", sa.String(length=80), nullable=True),
        sa.Column("severity", sa.String(length=80), nullable=True),
        sa.Column("route_mode", sa.String(length=30), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_repair_responsibility_grants"),
        sa.ForeignKeyConstraint(
            ["school_id", "responsibility_type_id"],
            ["responsibility_types.school_id", "responsibility_types.id"],
            name="fk_repair_responsibility_grants_type",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "responsibility_group_id"],
            ["responsibility_groups.school_id", "responsibility_groups.id"],
            name="fk_repair_responsibility_grants_group",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_repair_responsibility_grants_location",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "route_mode IN ('direct', 'group_claim', 'manager_dispatch', 'create_and_handle')",
            name="ck_repair_responsibility_grants_route_mode",
        ),
        sa.CheckConstraint(_CONFIRMATION_STATUSES, name="ck_repair_responsibility_grants_status"),
        sa.CheckConstraint(_VALID_INTERVAL, name="ck_repair_responsibility_grants_valid_interval"),
        sa.CheckConstraint("version >= 1", name="ck_repair_responsibility_grants_version_positive"),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("responsibility_type_id", "="),
            (
                sa.text("COALESCE(location_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
                "=",
            ),
            (sa.text("COALESCE(repair_subtype, '')"), "="),
            (sa.text("COALESCE(severity, '')"), "="),
            (_effective_range(), "&&"),
            where=sa.text("status = 'confirmed'"),
            using="gist",
            name="excl_repair_responsibility_grants_confirmed_scope_period",
        ),
    )
    op.create_index(
        "ix_repair_responsibility_grants_current_scope",
        "repair_responsibility_grants",
        [
            "school_id",
            "responsibility_type_id",
            "status",
            "location_id",
            "repair_subtype",
            "severity",
            "valid_from",
            "valid_to",
        ],
    )
    op.create_table(
        "command_idempotency_records",
        _uuid_column("id"),
        _uuid_column("school_id"),
        sa.Column("action_code", sa.String(length=120), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        _uuid_column("actor_person_id"),
        sa.Column("actor_context_type", sa.String(length=20), nullable=False),
        sa.Column("actor_context_key", sa.String(length=100), nullable=False),
        _uuid_column("actor_assignment_id", nullable=True),
        sa.Column("actor_assignment_version", sa.BigInteger(), nullable=True),
        _uuid_column("actor_guardian_relationship_id", nullable=True),
        sa.Column("actor_guardian_relationship_version", sa.BigInteger(), nullable=True),
        sa.Column("resource_type", sa.String(length=80), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'in_progress'"), nullable=False
        ),
        sa.Column("result_type", sa.String(length=80), nullable=True),
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_command_idempotency_records"),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_command_idempotency_records_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_command_idempotency_records_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "actor_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_command_idempotency_records_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "actor_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_command_idempotency_records_assignment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "actor_guardian_relationship_id"],
            ["guardian_relationships.school_id", "guardian_relationships.id"],
            name="fk_command_idempotency_records_guardian",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('in_progress', 'succeeded', 'terminal_failure')",
            name="ck_command_idempotency_records_status",
        ),
        sa.CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$' AND request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_command_idempotency_records_hashes",
        ),
        sa.CheckConstraint(
            "(actor_context_type = 'assignment' "
            "AND actor_assignment_id IS NOT NULL AND actor_assignment_version IS NOT NULL "
            "AND actor_assignment_version >= 1 "
            "AND actor_guardian_relationship_id IS NULL "
            "AND actor_guardian_relationship_version IS NULL "
            "AND actor_context_key = 'assignment:' || actor_assignment_id::text || ':' "
            "|| actor_assignment_version::text) OR "
            "(actor_context_type = 'guardian' "
            "AND actor_assignment_id IS NULL AND actor_assignment_version IS NULL "
            "AND actor_guardian_relationship_id IS NOT NULL "
            "AND actor_guardian_relationship_version IS NOT NULL "
            "AND actor_guardian_relationship_version >= 1 "
            "AND actor_context_key = 'guardian:' || actor_guardian_relationship_id::text || ':' "
            "|| actor_guardian_relationship_version::text)",
            name="ck_command_idempotency_records_actor_context",
        ),
        sa.CheckConstraint(
            "(status = 'in_progress' AND result_type IS NULL AND result_payload IS NULL) OR "
            "(status IN ('succeeded', 'terminal_failure') "
            "AND result_type IS NOT NULL AND result_payload IS NOT NULL)",
            name="ck_command_idempotency_records_result_state",
        ),
        sa.CheckConstraint(
            "result_payload IS NULL OR (jsonb_typeof(result_payload) = 'object' "
            "AND result_payload ?& ARRAY['resource_type','resource_id',"
            "'resource_version','outcome_code'] "
            "AND (result_payload - ARRAY['resource_type','resource_id','resource_version',"
            "'outcome_code']::text[]) = '{}'::jsonb "
            "AND jsonb_typeof(result_payload->'resource_type') = 'string' "
            "AND jsonb_typeof(result_payload->'resource_id') = 'string' "
            "AND jsonb_typeof(result_payload->'resource_version') = 'number' "
            "AND jsonb_typeof(result_payload->'outcome_code') = 'string' "
            "AND result_payload->>'resource_type' ~ '^[a-z][a-z0-9_.-]*$' "
            "AND ((result_payload->>'resource_id')::uuid)::text = "
            "result_payload->>'resource_id' "
            "AND (result_payload->>'resource_version')::bigint >= 1 "
            "AND result_payload->>'outcome_code' ~ '^[a-z][a-z0-9_.-]*$')",
            name="ck_command_idempotency_records_safe_result",
        ),
        sa.UniqueConstraint(
            "school_id",
            "action_code",
            "actor_context_key",
            "resource_type",
            "resource_id",
            "idempotency_key_hash",
            name="uq_command_idempotency_records_scope_key",
        ),
    )
    op.create_index(
        "ix_command_idempotency_records_lookup",
        "command_idempotency_records",
        [
            "school_id",
            "action_code",
            "actor_context_key",
            "resource_type",
            "resource_id",
            "idempotency_key_hash",
        ],
    )
    op.execute(
        """
        CREATE FUNCTION public.enforce_command_idempotency_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF ROW(
                OLD.id, OLD.school_id, OLD.action_code, OLD.actor_user_id,
                OLD.actor_person_id, OLD.actor_context_type, OLD.actor_context_key,
                OLD.actor_assignment_id, OLD.actor_assignment_version,
                OLD.actor_guardian_relationship_id,
                OLD.actor_guardian_relationship_version, OLD.resource_type,
                OLD.resource_id, OLD.idempotency_key_hash, OLD.request_sha256,
                OLD.created_at
            ) IS DISTINCT FROM ROW(
                NEW.id, NEW.school_id, NEW.action_code, NEW.actor_user_id,
                NEW.actor_person_id, NEW.actor_context_type, NEW.actor_context_key,
                NEW.actor_assignment_id, NEW.actor_assignment_version,
                NEW.actor_guardian_relationship_id,
                NEW.actor_guardian_relationship_version, NEW.resource_type,
                NEW.resource_id, NEW.idempotency_key_hash, NEW.request_sha256,
                NEW.created_at
            ) THEN
                RAISE EXCEPTION 'idempotency identity is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.status <> 'in_progress' THEN
                RAISE EXCEPTION 'idempotency result is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.status NOT IN ('succeeded', 'terminal_failure')
               OR NEW.result_type IS NULL OR NEW.result_payload IS NULL THEN
                RAISE EXCEPTION 'invalid idempotency terminal transition'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.enforce_command_idempotency_transition() FROM PUBLIC")
    op.execute(
        """
        CREATE TRIGGER enforce_command_idempotency_transition
        BEFORE UPDATE ON public.command_idempotency_records
        FOR EACH ROW EXECUTE FUNCTION public.enforce_command_idempotency_transition()
        """
    )
    op.create_table(
        "outbox_messages",
        _uuid_column("id"),
        _uuid_column("school_id"),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("aggregate_type", sa.String(length=80), nullable=False),
        _uuid_column("aggregate_id"),
        sa.Column("aggregate_version", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_messages"),
        sa.ForeignKeyConstraint(
            ["school_id"], ["schools.id"], name="fk_outbox_messages_school", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("school_id", "event_key", name="uq_outbox_messages_school_event_key"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'uncertain', 'dead_letter')",
            name="ck_outbox_messages_status",
        ),
        sa.CheckConstraint("aggregate_version >= 1", name="ck_outbox_messages_version_positive"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_outbox_messages_attempt_count"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object' "
            "AND payload ?& ARRAY['event_type','aggregate_type','aggregate_id',"
            "'aggregate_version','occurred_at'] "
            "AND (payload - ARRAY['event_type','aggregate_type','aggregate_id',"
            "'aggregate_version','occurred_at']::text[]) = '{}'::jsonb "
            "AND jsonb_typeof(payload->'event_type') = 'string' "
            "AND jsonb_typeof(payload->'aggregate_type') = 'string' "
            "AND jsonb_typeof(payload->'aggregate_id') = 'string' "
            "AND jsonb_typeof(payload->'aggregate_version') = 'number' "
            "AND jsonb_typeof(payload->'occurred_at') = 'string' "
            "AND payload->>'event_type' = event_type "
            "AND payload->>'aggregate_type' = aggregate_type "
            "AND payload->>'aggregate_id' = aggregate_id::text "
            "AND (payload->>'aggregate_version')::bigint = aggregate_version "
            "AND (payload->>'occurred_at')::timestamptz = occurred_at",
            name="ck_outbox_messages_safe_payload",
        ),
    )
    op.create_index(
        "ix_outbox_messages_pending",
        "outbox_messages",
        ["status", "available_at", "id"],
    )

    op.add_column("audit_logs", _uuid_column("school_id", nullable=True))
    op.add_column("audit_logs", _uuid_column("actor_person_id", nullable=True))
    op.add_column("audit_logs", _uuid_column("actor_assignment_id", nullable=True))
    op.add_column(
        "audit_logs", sa.Column("actor_assignment_version", sa.BigInteger(), nullable=True)
    )
    op.add_column("audit_logs", _uuid_column("actor_guardian_relationship_id", nullable=True))
    op.add_column(
        "audit_logs",
        sa.Column("actor_guardian_relationship_version", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "audit_logs", sa.Column("actor_context_type", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "audit_logs", sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True)
    )
    op.create_foreign_key(
        "fk_audit_logs_school",
        "audit_logs",
        "schools",
        ["school_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_audit_logs_school_person",
        "audit_logs",
        "school_people",
        ["school_id", "actor_person_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_audit_logs_school_assignment",
        "audit_logs",
        "staff_assignments",
        ["school_id", "actor_assignment_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_audit_logs_school_guardian",
        "audit_logs",
        "guardian_relationships",
        ["school_id", "actor_guardian_relationship_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_audit_logs_typed_business_context",
        "audit_logs",
        "school_id IS NULL OR (actor_person_id IS NOT NULL "
        "AND actor_context_type IN ('assignment', 'guardian') "
        "AND idempotency_key_hash ~ '^[0-9a-f]{64}$' "
        "AND ((actor_context_type = 'assignment' "
        "AND actor_assignment_id IS NOT NULL AND actor_assignment_version IS NOT NULL "
        "AND actor_assignment_version >= 1 "
        "AND actor_guardian_relationship_id IS NULL "
        "AND actor_guardian_relationship_version IS NULL) OR "
        "(actor_context_type = 'guardian' "
        "AND actor_assignment_id IS NULL AND actor_assignment_version IS NULL "
        "AND actor_guardian_relationship_id IS NOT NULL "
        "AND actor_guardian_relationship_version IS NOT NULL "
        "AND actor_guardian_relationship_version >= 1)))",
    )
    op.create_index("ix_audit_logs_school_created_at", "audit_logs", ["school_id", "created_at"])


def downgrade() -> None:
    """按依赖逆序移除 Task 5 第一阶段事务事实。"""

    op.drop_index("ix_audit_logs_school_created_at", table_name="audit_logs")
    op.drop_constraint("ck_audit_logs_typed_business_context", "audit_logs", type_="check")
    op.drop_constraint("fk_audit_logs_school_guardian", "audit_logs", type_="foreignkey")
    op.drop_constraint("fk_audit_logs_school_assignment", "audit_logs", type_="foreignkey")
    op.drop_constraint("fk_audit_logs_school_person", "audit_logs", type_="foreignkey")
    op.drop_constraint("fk_audit_logs_school", "audit_logs", type_="foreignkey")
    for column_name in (
        "idempotency_key_hash",
        "actor_context_type",
        "actor_guardian_relationship_version",
        "actor_guardian_relationship_id",
        "actor_assignment_version",
        "actor_assignment_id",
        "actor_person_id",
        "school_id",
    ):
        op.drop_column("audit_logs", column_name)

    op.drop_table("outbox_messages")
    op.drop_table("command_idempotency_records")
    op.execute("DROP FUNCTION public.enforce_command_idempotency_transition()")
    op.drop_table("repair_responsibility_grants")
    op.drop_table("responsibility_group_memberships")
    op.drop_table("responsibility_group_versions")
    op.drop_table("responsibility_groups")
    op.drop_table("responsibility_types")
