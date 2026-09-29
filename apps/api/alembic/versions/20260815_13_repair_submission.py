"""Create the bounded 7A repair submission facts.

Revision ID: 20260815_13
Revises: 20260809_12
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260815_13"
down_revision = "20260809_12"
branch_labels = None
depends_on = None

_TABLES = ("repair_scope_bindings", "repair_tickets")
_AUDIT_ARGUMENTS = (
    "integer,uuid,uuid,uuid,bigint,uuid,bigint,text,text,text,text,uuid,jsonb,jsonb,uuid,text"
)
_AUDIT_SIGNATURE = f"app.append_business_audit({_AUDIT_ARGUMENTS})"
_SAVED_AUDIT_SIGNATURE = f"app_private.append_business_audit_revision_12({_AUDIT_ARGUMENTS})"


def _effective_range() -> sa.TextClause:
    return sa.text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')")


def _create_scope_bindings() -> None:
    op.create_table(
        "repair_scope_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("school_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("scope_kind", sa.String(20), nullable=False),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "responsibility_group_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("responsibility_group_version", sa.BigInteger(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_repair_scope_bindings_school_id_id",
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_repair_scope_bindings_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_repair_scope_bindings_location",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "responsibility_group_id"],
            ["responsibility_groups.school_id", "responsibility_groups.id"],
            name="fk_repair_scope_bindings_group",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "purpose IN ('handler', 'general_affairs')",
            name="ck_repair_scope_bindings_purpose",
        ),
        sa.CheckConstraint(
            "scope_kind IN ('school', 'location')",
            name="ck_repair_scope_bindings_scope_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_repair_scope_bindings_status",
        ),
        sa.CheckConstraint(
            "(scope_kind = 'school' AND location_id IS NULL) OR "
            "(scope_kind = 'location' AND location_id IS NOT NULL)",
            name="ck_repair_scope_bindings_location_scope",
        ),
        sa.CheckConstraint(
            "purpose <> 'general_affairs' OR scope_kind = 'school'",
            name="ck_repair_scope_bindings_general_affairs_school",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_repair_scope_bindings_valid_interval",
        ),
        sa.CheckConstraint(
            "responsibility_group_version >= 1",
            name="ck_repair_scope_bindings_group_version",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_repair_scope_bindings_version",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("purpose", "="),
            ("scope_kind", "="),
            (
                sa.text("COALESCE(location_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
                "=",
            ),
            (_effective_range(), "&&"),
            where=sa.text("status = 'active'"),
            using="gist",
            name="excl_repair_scope_bindings_active_scope_period",
        ),
    )
    op.create_index(
        "ix_repair_scope_bindings_current_scope",
        "repair_scope_bindings",
        [
            "school_id",
            "purpose",
            "scope_kind",
            "location_id",
            "status",
            "valid_from",
            "valid_to",
        ],
    )


def _create_tickets() -> None:
    op.create_table(
        "repair_tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("public_ref", sa.String(64), nullable=False),
        sa.Column("school_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("reporter_person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "reporter_assignment_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("reporter_assignment_version", sa.BigInteger(), nullable=False),
        sa.Column("reporter_name_snapshot", sa.String(200), nullable=False),
        sa.Column("reporter_phone_snapshot", sa.String(32), nullable=False),
        sa.Column("location_text", sa.String(200), nullable=False),
        sa.Column(
            "recognized_location_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("problem_description", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("initial_route_kind", sa.String(30), nullable=False),
        sa.Column("initial_binding_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("initial_binding_version", sa.BigInteger(), nullable=True),
        sa.Column("initial_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("initial_group_version", sa.BigInteger(), nullable=True),
        sa.Column("initial_candidate_count", sa.BigInteger(), nullable=False),
        sa.Column("initial_resolution_branch", sa.String(80), nullable=False),
        sa.Column("initial_resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("designated_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "designated_assignment_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("designated_assignment_version", sa.BigInteger(), nullable=True),
        sa.Column(
            "current_handler_person_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "current_handler_assignment_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("current_handler_assignment_version", sa.BigInteger(), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_code", sa.String(80), nullable=True),
        sa.UniqueConstraint("school_id", "id", name="uq_repair_tickets_school_id_id"),
        sa.UniqueConstraint("public_ref", name="uq_repair_tickets_public_ref"),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_repair_tickets_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "reporter_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_repair_tickets_reporter_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "reporter_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_repair_tickets_reporter_assignment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "recognized_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_repair_tickets_location",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "initial_binding_id"],
            ["repair_scope_bindings.school_id", "repair_scope_bindings.id"],
            name="fk_repair_tickets_initial_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "initial_group_id"],
            ["responsibility_groups.school_id", "responsibility_groups.id"],
            name="fk_repair_tickets_initial_group",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "designated_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_repair_tickets_designated_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "designated_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_repair_tickets_designated_assignment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "current_handler_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_repair_tickets_handler_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "current_handler_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_repair_tickets_handler_assignment",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_repair_tickets_revision"),
        sa.CheckConstraint(
            "reporter_assignment_version >= 1",
            name="ck_repair_tickets_reporter_assignment_version",
        ),
        sa.CheckConstraint(
            "btrim(reporter_name_snapshot) <> '' "
            "AND btrim(reporter_phone_snapshot) <> '' "
            "AND btrim(location_text) <> '' "
            "AND btrim(problem_description) <> ''",
            name="ck_repair_tickets_required_text",
        ),
        sa.CheckConstraint(
            "result_code IS NULL OR btrim(result_code) <> ''",
            name="ck_repair_tickets_result_nonblank",
        ),
        sa.CheckConstraint(
            "status IN ('submitted', 'processing', 'completed')",
            name="ck_repair_tickets_status",
        ),
        sa.CheckConstraint(
            "initial_route_kind IN ('ordinary', 'general_affairs', 'unresolved')",
            name="ck_repair_tickets_initial_route_kind",
        ),
        sa.CheckConstraint(
            "initial_resolution_branch IN ("
            "'location_handler', 'school_handler_after_location_zero', "
            "'school_handler_without_location', 'general_affairs', 'unresolved')",
            name="ck_repair_tickets_initial_resolution_branch",
        ),
        sa.CheckConstraint(
            "(initial_route_kind = 'ordinary' AND initial_resolution_branch IN ("
            "'location_handler', 'school_handler_after_location_zero', "
            "'school_handler_without_location')) OR "
            "(initial_route_kind = 'general_affairs' "
            "AND initial_resolution_branch = 'general_affairs') OR "
            "(initial_route_kind = 'unresolved' "
            "AND initial_resolution_branch = 'unresolved')",
            name="ck_repair_tickets_initial_route_branch",
        ),
        sa.CheckConstraint(
            "initial_candidate_count >= 0",
            name="ck_repair_tickets_initial_candidate_count",
        ),
        sa.CheckConstraint(
            "(initial_route_kind = 'unresolved' "
            "AND initial_binding_id IS NULL "
            "AND initial_binding_version IS NULL "
            "AND initial_group_id IS NULL "
            "AND initial_group_version IS NULL "
            "AND initial_candidate_count = 0) OR "
            "(initial_route_kind IN ('ordinary', 'general_affairs') "
            "AND initial_binding_id IS NOT NULL "
            "AND initial_binding_version >= 1 "
            "AND initial_group_id IS NOT NULL "
            "AND initial_group_version >= 1 "
            "AND initial_candidate_count >= 1)",
            name="ck_repair_tickets_initial_route_proof",
        ),
        sa.CheckConstraint(
            "(designated_person_id IS NULL "
            "AND designated_assignment_id IS NULL "
            "AND designated_assignment_version IS NULL) OR "
            "(designated_person_id IS NOT NULL "
            "AND designated_assignment_id IS NOT NULL "
            "AND designated_assignment_version >= 1)",
            name="ck_repair_tickets_designated_triplet",
        ),
        sa.CheckConstraint(
            "(current_handler_person_id IS NULL "
            "AND current_handler_assignment_id IS NULL "
            "AND current_handler_assignment_version IS NULL) OR "
            "(current_handler_person_id IS NOT NULL "
            "AND current_handler_assignment_id IS NOT NULL "
            "AND current_handler_assignment_version >= 1)",
            name="ck_repair_tickets_current_handler_triplet",
        ),
        sa.CheckConstraint(
            "(status = 'submitted' "
            "AND current_handler_person_id IS NULL "
            "AND started_at IS NULL "
            "AND completed_at IS NULL "
            "AND result_code IS NULL) OR "
            "(status = 'processing' "
            "AND current_handler_person_id IS NOT NULL "
            "AND started_at IS NOT NULL "
            "AND completed_at IS NULL "
            "AND result_code IS NULL) OR "
            "(status = 'completed' "
            "AND current_handler_person_id IS NOT NULL "
            "AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL "
            "AND result_code IS NOT NULL)",
            name="ck_repair_tickets_status_facts",
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR started_at >= submitted_at",
            name="ck_repair_tickets_start_after_submit",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR (started_at IS NOT NULL AND completed_at >= started_at)",
            name="ck_repair_tickets_completion_after_start",
        ),
    )
    op.create_index(
        "ix_repair_tickets_reporter",
        "repair_tickets",
        ["school_id", "reporter_person_id", "submitted_at", "id"],
    )


def _create_rls_and_runtime_acl() -> None:
    owner = "CURRENT_USER = 'campus_ai_db_owner'"
    tenant = "school_id = (SELECT app.require_school_context())"
    command = "current_setting('app.context_purpose', true) = 'command'"
    for table_name in _TABLES:
        op.execute(f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_select ON public.{table_name} FOR SELECT "
            f"USING (CASE WHEN {owner} THEN true ELSE {tenant} END)"
        )
        op.execute(
            f"CREATE POLICY tenant_insert ON public.{table_name} FOR INSERT "
            f"WITH CHECK (CASE WHEN {owner} THEN true ELSE {tenant} AND {command} END)"
        )
        op.execute(
            f"CREATE POLICY tenant_update ON public.{table_name} FOR UPDATE "
            f"USING (CASE WHEN {owner} THEN true ELSE {tenant} AND {command} END) "
            f"WITH CHECK (CASE WHEN {owner} THEN true ELSE {tenant} AND {command} END)"
        )
        op.execute(f"CREATE POLICY owner_delete ON public.{table_name} FOR DELETE USING ({owner})")
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE public.{table_name} "
            "FROM PUBLIC, campus_ai_runtime CASCADE"
        )
    op.execute("GRANT SELECT, UPDATE (id) ON public.repair_scope_bindings TO campus_ai_runtime")
    op.execute("GRANT SELECT, INSERT ON public.repair_tickets TO campus_ai_runtime")


def _extend_audit_allowlist() -> None:
    definition = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT pg_catalog.pg_get_functiondef(%s::pg_catalog.regprocedure)",
            (_AUDIT_SIGNATURE,),
        )
        .scalar_one()
    )
    if not isinstance(definition, str):
        raise RuntimeError("cannot read revision-12 business audit function")
    old = """IF p_event_type !~ '^[a-z][a-z0-9_.-]*$'
               OR p_event_type IS DISTINCT FROM 'master_data.assignment.changed'
               OR p_target_type IS DISTINCT FROM 'staff_assignment'
               OR p_reason_code IS DISTINCT FROM 'manual.assignment-confirmed'
               OR NOT app_private.is_safe_business_audit_summary(p_before_summary)
               OR NOT app_private.is_safe_business_audit_summary(p_after_summary) THEN"""
    new = """IF NOT (
                    (p_event_type ~ '^[a-z][a-z0-9_.-]*$'
                     AND p_event_type = 'master_data.assignment.changed'
                     AND p_target_type = 'staff_assignment'
                     AND p_reason_code = 'manual.assignment-confirmed'
                     AND app_private.is_safe_business_audit_summary(p_before_summary)
                     AND app_private.is_safe_business_audit_summary(p_after_summary))
                 OR (p_event_type = 'repair.ticket.submitted'
                     AND p_target_type = 'repair_ticket'
                     AND p_reason_code = 'manual.repair-submitted'
                     AND p_before_summary = pg_catalog.jsonb_build_object(
                         'state_code', 'repair.absent',
                         'resource_version', NULL,
                         'stable_reference_ids',
                             pg_catalog.jsonb_build_array(p_target_id::text),
                         'fact_codes', '[]'::jsonb
                     )
                     AND p_after_summary = pg_catalog.jsonb_build_object(
                         'state_code', 'repair.submitted',
                         'resource_version', 1,
                         'stable_reference_ids',
                             pg_catalog.jsonb_build_array(p_target_id::text),
                         'fact_codes', pg_catalog.jsonb_build_array('repair.submitted')
                     ))
               ) THEN"""
    if definition.count(old) != 1:
        raise RuntimeError("revision-12 business audit function shape changed")

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app, app_private TO campus_ai_audit_definer")
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    op.execute(f"ALTER FUNCTION {_AUDIT_SIGNATURE} RENAME TO append_business_audit_revision_12")
    op.execute(
        "ALTER FUNCTION app.append_business_audit_revision_12("
        f"{_AUDIT_ARGUMENTS}) SET SCHEMA app_private"
    )
    op.execute(
        f"REVOKE ALL PRIVILEGES ON FUNCTION {_SAVED_AUDIT_SIGNATURE} "
        "FROM PUBLIC, campus_ai_runtime, campus_ai_db_owner, campus_ai_migration, "
        "campus_ai_context_definer, campus_ai_outbox_probe_definer, "
        "campus_ai_takeover_definer CASCADE"
    )
    op.execute(definition.replace(old, new))
    op.execute(f"REVOKE ALL ON FUNCTION {_AUDIT_SIGNATURE} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {_AUDIT_SIGNATURE} TO campus_ai_runtime, campus_ai_db_owner"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_audit_definer")


def upgrade() -> None:
    _create_scope_bindings()
    _create_tickets()
    _create_rls_and_runtime_acl()
    _extend_audit_allowlist()


def downgrade() -> None:
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app, app_private TO campus_ai_audit_definer")
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    op.execute(f"DROP FUNCTION {_AUDIT_SIGNATURE}")
    op.execute(f"ALTER FUNCTION {_SAVED_AUDIT_SIGNATURE} SET SCHEMA app")
    op.execute(
        "ALTER FUNCTION app.append_business_audit_revision_12("
        f"{_AUDIT_ARGUMENTS}) RENAME TO append_business_audit"
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON FUNCTION {_AUDIT_SIGNATURE} FROM PUBLIC CASCADE")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {_AUDIT_SIGNATURE} TO campus_ai_runtime, campus_ai_db_owner"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_audit_definer")
    for table_name in reversed(_TABLES):
        op.drop_table(table_name)
