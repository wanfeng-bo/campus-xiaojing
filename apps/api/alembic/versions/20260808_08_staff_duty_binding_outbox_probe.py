"""add narrow outbox verification for staff duty binding recovery

Revision ID: 20260808_08
Revises: 20260805_07
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op

revision = "20260808_08"
down_revision = "20260805_07"
branch_labels = None
depends_on = None

_SIGNATURE = "app.verify_staff_duty_binding_outbox(uuid,uuid,uuid,bigint)"
_AUDIT_TRANSITION_CONSTRAINT = "ck_audit_logs_staff_assignment_confirmed_transition"


def _replace_audit_summary_validator(*, allow_absent: bool) -> None:
    """按迁移方向收敛类型化审计状态白名单，不改写任何历史审计。"""

    state_check = (
        "NOT IN ('assignment.absent', 'assignment.confirmed')"
        if allow_absent
        else "IS DISTINCT FROM 'assignment.confirmed'"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app_private TO campus_ai_audit_definer")
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION app_private.is_safe_business_audit_summary(
            p_summary jsonb
        )
        RETURNS boolean
        LANGUAGE plpgsql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF p_summary IS NULL
               OR jsonb_typeof(p_summary) IS DISTINCT FROM 'object'
               OR NOT p_summary ?& ARRAY[
                    'state_code', 'resource_version', 'stable_reference_ids', 'fact_codes'
               ]
               OR (SELECT count(*) FROM jsonb_object_keys(p_summary)) <> 4
               OR jsonb_typeof(p_summary -> 'state_code') IS DISTINCT FROM 'string'
               OR (p_summary ->> 'state_code') {state_check}
               OR (
                    jsonb_typeof(p_summary -> 'resource_version') IS DISTINCT FROM 'null'
                    AND (
                        jsonb_typeof(p_summary -> 'resource_version') IS DISTINCT FROM 'number'
                        OR (p_summary ->> 'resource_version') !~ '^[1-9][0-9]*$'
                    )
               )
               OR (
                    (p_summary ->> 'state_code') = 'assignment.absent'
                    AND jsonb_typeof(p_summary -> 'resource_version') IS DISTINCT FROM 'null'
               )
               OR jsonb_typeof(p_summary -> 'stable_reference_ids') IS DISTINCT FROM 'array'
               OR jsonb_array_length(p_summary -> 'stable_reference_ids') > 16
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(
                        p_summary -> 'stable_reference_ids'
                    ) AS value(item)
                    WHERE jsonb_typeof(value.item) IS DISTINCT FROM 'string'
                       OR (value.item #>> '{{}}')
                          !~* '^[0-9a-f]{{8}}(-[0-9a-f]{{4}}){{3}}-[0-9a-f]{{12}}$'
               )
               OR jsonb_typeof(p_summary -> 'fact_codes') IS DISTINCT FROM 'array'
               OR jsonb_array_length(p_summary -> 'fact_codes') > 32
               OR (
                    (p_summary ->> 'state_code') = 'assignment.absent'
                    AND jsonb_array_length(p_summary -> 'fact_codes') <> 0
               )
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(p_summary -> 'fact_codes') AS value(item)
                    WHERE jsonb_typeof(value.item) IS DISTINCT FROM 'string'
                       OR (value.item #>> '{{}}') IS DISTINCT FROM 'assignment.updated'
               ) THEN
                RETURN false;
            END IF;
            RETURN true;
        END
        $function$
        """
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app_private FROM campus_ai_audit_definer")


def upgrade() -> None:
    """创建只返回布尔值、必须绑定可信命令上下文的 Outbox 核验函数。"""

    _replace_audit_summary_validator(allow_absent=True)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        f"""
        ALTER TABLE public.audit_logs
        ADD CONSTRAINT {_AUDIT_TRANSITION_CONSTRAINT}
        CHECK (
            event_type IS DISTINCT FROM 'master_data.assignment.changed'
            OR target_type IS DISTINCT FROM 'staff_assignment'
            OR reason IS DISTINCT FROM 'manual.assignment-confirmed'
            OR (after_summary ->> 'state_code') = 'assignment.confirmed'
        )
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO campus_ai_outbox_probe_definer")
    op.execute(
        "GRANT SELECT (id, school_id, event_key, event_type, aggregate_type, "
        "aggregate_id, aggregate_version) ON TABLE public.outbox_messages "
        "TO campus_ai_outbox_probe_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.require_school_context() TO campus_ai_outbox_probe_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT USAGE, CREATE ON SCHEMA app TO campus_ai_outbox_probe_definer")
    op.execute("SET LOCAL ROLE campus_ai_outbox_probe_definer")
    op.execute("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC")
    op.execute(
        """
        CREATE FUNCTION app.verify_staff_duty_binding_outbox(
            p_event_id uuid,
            p_school_id uuid,
            p_assignment_id uuid,
            p_assignment_version bigint
        )
        RETURNS boolean
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_school_id uuid;
        BEGIN
            v_school_id := app.require_school_context();
            IF current_setting('app.context_purpose', true) IS DISTINCT FROM 'command'
               OR current_setting('app.idempotency_key_hash', true)
                  !~ '^[0-9a-f]{64}$'
               OR p_school_id IS DISTINCT FROM v_school_id
               OR p_assignment_version < 1 THEN
                RETURN false;
            END IF;
            RETURN EXISTS (
                SELECT 1
                FROM public.outbox_messages AS message
                WHERE message.id = p_event_id
                  AND message.school_id = v_school_id
                  AND message.event_key =
                      'master-data.staff-duty-binding:' || p_assignment_id::text
                  AND message.event_type = 'master_data.assignment.changed'
                  AND message.aggregate_type = 'staff_assignment'
                  AND message.aggregate_id = p_assignment_id
                  AND message.aggregate_version = p_assignment_version
            );
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO campus_ai_runtime")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_outbox_probe_definer")


def downgrade() -> None:
    """删除 Task 7 的窄 Outbox 核验能力。"""

    op.execute("SET LOCAL ROLE campus_ai_outbox_probe_definer")
    op.execute(f"DROP FUNCTION IF EXISTS {_SIGNATURE}")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION app.require_school_context() "
        "FROM campus_ai_outbox_probe_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "REVOKE SELECT (id, school_id, event_key, event_type, aggregate_type, "
        "aggregate_id, aggregate_version) ON TABLE public.outbox_messages "
        "FROM campus_ai_outbox_probe_definer"
    )
    op.execute("REVOKE USAGE ON SCHEMA public, app FROM campus_ai_outbox_probe_definer")
    op.execute(
        f"ALTER TABLE public.audit_logs DROP CONSTRAINT IF EXISTS {_AUDIT_TRANSITION_CONSTRAINT}"
    )
    _replace_audit_summary_validator(allow_absent=False)
