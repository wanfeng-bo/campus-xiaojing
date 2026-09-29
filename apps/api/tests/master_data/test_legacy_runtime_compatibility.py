from __future__ import annotations

import uuid

import psycopg
import pytest

from tests.master_data.conftest import PostgresRoles


@pytest.mark.integration
def test_runtime_keeps_exact_legacy_identity_and_authorization_paths(
    current_master_data_schema: PostgresRoles,
) -> None:
    """RLS 加固后旧登录和集中授权所需的最小 DML 仍必须可用。"""

    marker = uuid.uuid4().hex
    external_subject = f"task5-runtime:{marker}"
    role_code = f"task5_role_{marker}"

    with psycopg.connect(
        **current_master_data_schema.runtime_connection_parameters,
        autocommit=False,
    ) as connection:
        user_id = connection.execute(
            """
            INSERT INTO public.users (external_subject, display_name, is_active)
            VALUES (%s, %s, true)
            RETURNING id
            """,
            (external_subject, "Task 5 兼容用户"),
        ).fetchone()
        assert user_id is not None
        role_id = connection.execute(
            """
            INSERT INTO public.roles (code, name, is_system)
            VALUES (%s, %s, false)
            RETURNING id
            """,
            (role_code, "Task 5 兼容角色"),
        ).fetchone()
        assert role_id is not None
        connection.execute(
            """
            INSERT INTO public.user_roles (user_id, role_id, assigned_by_user_id)
            VALUES (%s, %s, %s)
            """,
            (user_id[0], role_id[0], user_id[0]),
        )
        permission_id = connection.execute(
            """
            INSERT INTO public.role_permissions (role_id, action_code, scope_type)
            VALUES (%s, 'system.access.write', 'self')
            RETURNING id
            """,
            (role_id[0],),
        ).fetchone()
        assert permission_id is not None
        scope_id = connection.execute(
            """
            INSERT INTO public.responsibility_scopes (
                user_id, scope_type, scope_value, assigned_by_user_id
            )
            VALUES (%s, 'self', %s, %s)
            RETURNING id
            """,
            (user_id[0], str(user_id[0]), user_id[0]),
        ).fetchone()
        assert scope_id is not None

        assert connection.execute(
            "SELECT is_active FROM public.users WHERE id = %s",
            (user_id[0],),
        ).fetchone() == (True,)
        assert connection.execute(
            """
            SELECT permission.action_code, permission.scope_type
            FROM public.role_permissions AS permission
            JOIN public.user_roles AS membership ON membership.role_id = permission.role_id
            WHERE membership.user_id = %s
            """,
            (user_id[0],),
        ).fetchone() == ("system.access.write", "self")

        connection.execute(
            "DELETE FROM public.responsibility_scopes WHERE id = %s",
            (scope_id[0],),
        )
        connection.execute(
            "DELETE FROM public.role_permissions WHERE id = %s",
            (permission_id[0],),
        )
        connection.execute(
            "DELETE FROM public.user_roles WHERE user_id = %s AND role_id = %s",
            (user_id[0], role_id[0]),
        )
        connection.rollback()


@pytest.mark.integration
def test_runtime_cannot_create_formal_assignment_or_mutate_audit(
    current_master_data_schema: PostgresRoles,
) -> None:
    """旧技术授权兼容不能变成新业务任职或审计改写通道。"""

    with psycopg.connect(
        **current_master_data_schema.runtime_connection_parameters,
        autocommit=False,
    ) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                """
                INSERT INTO public.staff_assignments (
                    id, school_id, person_id, organization_unit_id, semantic_role_code,
                    position_title, appointment_source, approval_reference,
                    valid_from, status, version
                )
                VALUES (
                    gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), gen_random_uuid(),
                    'repair_worker', '维修工', 'manual', 'task5-negative-test',
                    CURRENT_TIMESTAMP, 'confirmed', 1
                )
                """
            )
        connection.rollback()

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("UPDATE public.audit_logs SET reason = 'forged'")
        connection.rollback()

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("TRUNCATE TABLE public.users")
