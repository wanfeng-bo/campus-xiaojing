from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import CheckConstraint, Table

from app.domains.repairs.models import (
    RepairScopeBinding,
    RepairTicket,
    new_repair_public_ref,
)
from tests.master_data.conftest import PostgresRoles

_SAVED_AUDIT_SIGNATURE = (
    "app_private.append_business_audit_revision_12("
    "integer,uuid,uuid,uuid,bigint,uuid,bigint,text,text,text,text,uuid,"
    "jsonb,jsonb,uuid,text)"
)
_RETIRED_REPAIR_TABLES = (
    "repair_responsibility_grants",
    "repair_scope_bindings",
    "repair_tickets",
    "responsibility_group_memberships",
    "responsibility_group_versions",
    "responsibility_groups",
    "responsibility_types",
)


def test_repair_submission_models_expose_exact_two_tables() -> None:
    assert RepairScopeBinding.__tablename__ == "repair_scope_bindings"
    assert RepairTicket.__tablename__ == "repair_tickets"


def test_repair_ticket_phone_metadata_is_explicitly_optional() -> None:
    table = cast(Table, RepairTicket.__table__)
    phone = table.c.reporter_phone_snapshot
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }

    assert phone.nullable is True
    assert "reporter_phone_snapshot" not in checks["ck_repair_tickets_required_text"]
    assert (
        checks["ck_repair_tickets_phone_nonblank"]
        == "reporter_phone_snapshot IS NULL OR btrim(reporter_phone_snapshot) <> ''"
    )


def test_public_references_are_random_and_do_not_embed_internal_ids() -> None:
    internal_id = uuid4()
    references = {new_repair_public_ref() for _ in range(20)}

    assert len(references) == 20
    assert all(20 <= len(reference) <= 64 for reference in references)
    assert all(str(internal_id) not in reference for reference in references)


def test_retired_repair_tables_have_force_rls_and_zero_runtime_acl(
    current_master_data_schema: PostgresRoles,
) -> None:
    with current_master_data_schema.admin_connection() as connection:
        relation_rows = connection.execute(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity
            FROM pg_class
            WHERE oid IN (
                'public.repair_scope_bindings'::regclass,
                'public.repair_tickets'::regclass
            )
            ORDER BY relname
            """
        ).fetchall()
        policy_rows = connection.execute(
            """
            SELECT tablename, policyname, cmd
            FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename IN ('repair_scope_bindings', 'repair_tickets')
            ORDER BY tablename, policyname
            """
        ).fetchall()
        privileges = connection.execute(
            """
            SELECT
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_scope_bindings', 'SELECT'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_scope_bindings', 'INSERT'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_scope_bindings', 'UPDATE'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_scope_bindings', 'DELETE'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_scope_bindings', 'TRUNCATE'),
                has_column_privilege('campus_ai_runtime',
                                     'public.repair_scope_bindings', 'id', 'UPDATE'),
                has_column_privilege('campus_ai_runtime',
                                     'public.repair_scope_bindings', 'status', 'UPDATE'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_tickets', 'SELECT'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_tickets', 'INSERT'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_tickets', 'UPDATE'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_tickets', 'DELETE'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_tickets', 'TRUNCATE')
            """
        ).fetchone()

    assert relation_rows == [
        ("repair_scope_bindings", True, True),
        ("repair_tickets", True, True),
    ]
    assert policy_rows == [
        (table_name, "sf5b_anchor_guard", "ALL")
        for table_name in ("repair_scope_bindings", "repair_tickets")
    ]
    assert privileges == (
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    )


def _seed_repair_parents(
    connection: psycopg.Connection[Any],
) -> dict[str, Any]:
    marker = uuid4().hex
    school_id = uuid4()
    person_id = uuid4()
    assignment_id = uuid4()
    account_binding_id = uuid4()
    unit_id = uuid4()
    group_id = uuid4()
    location_id = uuid4()
    valid_from = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
    external_subject = f"repair:{marker}"
    relationship_source_id = uuid4()
    relationship_source_ref = f"test:repair-schema-relationships:{marker}"

    connection.execute(
        """
        INSERT INTO schools (
            id, school_code, display_name, timezone_name, status, version
        ) VALUES (%s, %s, %s, 'Asia/Shanghai', 'active', 1)
        """,
        (school_id, f"repair-primary-{marker}", "报修测试学校-primary"),
    )
    user_row = connection.execute(
        """
        INSERT INTO users (external_subject, display_name, is_active, security_version)
        VALUES (%s, '报修测试人员', true, 1)
        RETURNING id
        """,
        (external_subject,),
    ).fetchone()
    assert user_row is not None
    actor_user_id = int(user_row[0])
    connection.execute(
        """
        INSERT INTO school_people (id, school_id, status, version)
        VALUES (%s, %s, 'active', 1)
        """,
        (person_id, school_id),
    )
    connection.execute(
        """
        INSERT INTO account_person_bindings (
            id, school_id, account_user_id, person_id, valid_from, status, version
        ) VALUES (%s, %s, %s, %s, %s, 'confirmed', 1)
        """,
        (
            account_binding_id,
            school_id,
            actor_user_id,
            person_id,
            valid_from,
        ),
    )
    connection.execute(
        """
        INSERT INTO organization_units (id, school_id, kind, status, version)
        VALUES (%s, %s, 'department', 'active', 1)
        """,
        (unit_id, school_id),
    )
    connection.execute(
        """
        INSERT INTO staff_assignments (
            id, school_id, person_id, organization_unit_id, position_title,
            semantic_role_code, appointment_source, approval_reference,
            valid_from, status, version
        ) VALUES (
            %s, %s, %s, %s, '维修人员', 'repair_worker', 'manual', %s,
            %s, 'confirmed', 1
        )
        """,
        (assignment_id, school_id, person_id, unit_id, f"repair-{marker}", valid_from),
    )
    has_relationship_content_slices = connection.execute(
        """
        SELECT to_regclass('public.account_person_binding_versions') IS NOT NULL
           AND to_regclass('public.staff_assignment_versions') IS NOT NULL
        """
    ).fetchone() == (True,)
    if has_relationship_content_slices:
        connection.execute(
            """
            INSERT INTO school_fact_sources (
                id, school_id, source_key, source_kind, source_ref, schema_version
            ) VALUES (%s, %s, %s, 'legacy', %s, 1)
            """,
            (
                relationship_source_id,
                school_id,
                relationship_source_ref,
                relationship_source_ref,
            ),
        )
        connection.execute(
            """
            INSERT INTO account_person_binding_versions (
                id, school_id, binding_id, account_user_id, person_id,
                provider_code, provider_tenant_key, external_subject,
                effective_from, status, target_version, source_ref, source_id
            ) VALUES (
                %s, %s, %s, %s, %s,
                'legacy', 'legacy', %s,
                %s, 'confirmed', 1, %s, %s
            )
            """,
            (
                uuid4(),
                school_id,
                account_binding_id,
                actor_user_id,
                person_id,
                external_subject,
                valid_from,
                relationship_source_ref,
                relationship_source_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO staff_assignment_versions (
                id, school_id, assignment_id, person_id, organization_unit_id,
                position_title, semantic_role_code, assignment_kind,
                appointment_source, approval_reference, effective_from,
                status, target_version, source_ref, source_id
            ) VALUES (
                %s, %s, %s, %s, %s,
                '维修人员', 'repair_worker', 'substantive',
                'manual', %s, %s,
                'confirmed', 1, %s, %s
            )
            """,
            (
                uuid4(),
                school_id,
                assignment_id,
                person_id,
                unit_id,
                f"repair-{marker}",
                valid_from,
                relationship_source_ref,
                relationship_source_id,
            ),
        )
    connection.execute(
        """
        INSERT INTO responsibility_groups (
            id, school_id, code, manager_assignment_id,
            manager_assignment_version, status, version
        ) VALUES (%s, %s, %s, %s, 1, 'active', 1)
        """,
        (group_id, school_id, f"repair-workers-{marker}", assignment_id),
    )
    connection.execute(
        """
        INSERT INTO campus_locations (
            id, school_id, parent_location_id, kind, status, version
        ) VALUES (%s, %s, NULL, 'campus', 'active', 1)
        """,
        (location_id, school_id),
    )
    return {
        "school_id": school_id,
        "person_id": person_id,
        "actor_user_id": actor_user_id,
        "assignment_id": assignment_id,
        "group_id": group_id,
        "location_id": location_id,
        "valid_from": valid_from,
    }


def _insert_unresolved_ticket(
    connection: psycopg.Connection[Any],
    facts: dict[str, Any],
    **overrides: object,
) -> None:
    values: dict[str, object] = {
        "id": uuid4(),
        "public_ref": new_repair_public_ref(),
        "school_id": facts["school_id"],
        "reporter_person_id": facts["person_id"],
        "reporter_assignment_id": facts["assignment_id"],
        "reporter_assignment_version": 1,
        "reporter_name_snapshot": "王老师",
        "reporter_phone_snapshot": "13800000000",
        "location_text": "女教职工宿舍楼后面菜地",
        "problem_description": "水管漏水",
        "status": "submitted",
        "initial_route_kind": "unresolved",
        "initial_candidate_count": 0,
        "initial_resolution_branch": "unresolved",
        "initial_resolved_at": datetime.now(UTC),
    }
    values.update(overrides)
    connection.execute(
        """
        INSERT INTO repair_tickets (
            id, public_ref, school_id, reporter_person_id, reporter_assignment_id,
            reporter_assignment_version, reporter_name_snapshot,
            reporter_phone_snapshot, location_text, problem_description, status,
            initial_route_kind, initial_candidate_count, initial_resolution_branch,
            initial_resolved_at, current_handler_person_id,
            current_handler_assignment_id, current_handler_assignment_version,
            started_at, completed_at, result_code
        ) VALUES (
            %(id)s, %(public_ref)s, %(school_id)s, %(reporter_person_id)s,
            %(reporter_assignment_id)s, %(reporter_assignment_version)s,
            %(reporter_name_snapshot)s, %(reporter_phone_snapshot)s,
            %(location_text)s, %(problem_description)s, %(status)s,
            %(initial_route_kind)s, %(initial_candidate_count)s,
            %(initial_resolution_branch)s, %(initial_resolved_at)s,
            %(current_handler_person_id)s, %(current_handler_assignment_id)s,
            %(current_handler_assignment_version)s, %(started_at)s,
            %(completed_at)s, %(result_code)s
        )
        """,
        {
            "current_handler_person_id": None,
            "current_handler_assignment_id": None,
            "current_handler_assignment_version": None,
            "started_at": None,
            "completed_at": None,
            "result_code": None,
            **values,
        },
    )


def test_all_retired_repair_tables_have_force_rls_anchor_guard_and_zero_runtime_acl(
    current_master_data_schema: PostgresRoles,
) -> None:
    with current_master_data_schema.admin_connection() as connection:
        relation_rows = connection.execute(
            """
            SELECT relation.relname, relation.relrowsecurity, relation.relforcerowsecurity
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname = ANY(%s)
            ORDER BY relation.relname
            """,
            (list(_RETIRED_REPAIR_TABLES),),
        ).fetchall()
        policy_rows = connection.execute(
            """
            SELECT tablename, policyname, cmd
            FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename = ANY(%s)
            ORDER BY tablename, policyname
            """,
            (list(_RETIRED_REPAIR_TABLES),),
        ).fetchall()
        runtime_acl = connection.execute(
            """
            SELECT 'table', table_name, NULL::text, privilege_type
            FROM information_schema.role_table_grants
            WHERE table_schema = 'public'
              AND grantee = 'campus_ai_runtime'
              AND table_name = ANY(%s)
            UNION ALL
            SELECT 'column', table_name, column_name, privilege_type
            FROM information_schema.column_privileges
            WHERE table_schema = 'public'
              AND grantee = 'campus_ai_runtime'
              AND table_name = ANY(%s)
            ORDER BY 1, 2, 3 NULLS FIRST, 4
            """,
            (list(_RETIRED_REPAIR_TABLES), list(_RETIRED_REPAIR_TABLES)),
        ).fetchall()

    assert relation_rows == [(table_name, True, True) for table_name in _RETIRED_REPAIR_TABLES]
    assert policy_rows == [
        (table_name, "sf5b_anchor_guard", "ALL") for table_name in _RETIRED_REPAIR_TABLES
    ]
    assert runtime_acl == []


def test_bootstrap_twice_converges_retired_repair_runtime_acl_to_zero(
    current_master_data_schema: PostgresRoles,
) -> None:
    with current_master_data_schema.admin_connection() as connection:
        connection.execute(
            "GRANT ALL PRIVILEGES ON public.repair_scope_bindings TO campus_ai_runtime"
        )
        connection.execute("GRANT ALL PRIVILEGES ON public.repair_tickets TO campus_ai_runtime")
        connection.execute(f"ALTER FUNCTION {_SAVED_AUDIT_SIGNATURE} OWNER TO campus_ai_db_owner")
        connection.execute(f"ALTER FUNCTION {_SAVED_AUDIT_SIGNATURE} SECURITY INVOKER")
        connection.execute(f"ALTER FUNCTION {_SAVED_AUDIT_SIGNATURE} SET search_path = public")
        connection.execute(
            f"GRANT EXECUTE ON FUNCTION {_SAVED_AUDIT_SIGNATURE} "
            "TO PUBLIC, campus_ai_runtime, campus_ai_context_definer"
        )

    assert current_master_data_schema.rerun_bootstrap().returncode == 0
    assert current_master_data_schema.rerun_bootstrap().returncode == 0

    with current_master_data_schema.admin_connection() as connection:
        privileges = connection.execute(
            """
            SELECT
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_scope_bindings', 'SELECT'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_scope_bindings', 'INSERT'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_scope_bindings', 'UPDATE'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_scope_bindings', 'DELETE'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_scope_bindings', 'TRUNCATE'),
                has_column_privilege('campus_ai_runtime',
                                     'public.repair_scope_bindings', 'id', 'UPDATE'),
                has_column_privilege('campus_ai_runtime',
                                     'public.repair_scope_bindings', 'status', 'UPDATE'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_tickets', 'SELECT'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_tickets', 'INSERT'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_tickets', 'UPDATE'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_tickets', 'DELETE'),
                has_table_privilege('campus_ai_runtime',
                                    'public.repair_tickets', 'TRUNCATE')
            """
        ).fetchone()
        saved_audit_function = connection.execute(
            """
            SELECT
                pg_get_userbyid(procedure.proowner),
                procedure.prosecdef,
                procedure.proconfig,
                has_function_privilege(
                    'campus_ai_runtime', procedure.oid, 'EXECUTE'
                ),
                has_function_privilege(
                    'campus_ai_db_owner', procedure.oid, 'EXECUTE'
                ),
                has_function_privilege(
                    'campus_ai_context_definer', procedure.oid, 'EXECUTE'
                ),
                EXISTS (
                    SELECT 1
                    FROM aclexplode(
                        COALESCE(
                            procedure.proacl,
                            acldefault('f', procedure.proowner)
                        )
                    ) AS expanded
                    WHERE expanded.grantee = 0
                      AND expanded.privilege_type = 'EXECUTE'
                )
            FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname = 'app_private'
              AND procedure.proname = 'append_business_audit_revision_12'
              AND procedure.pronargs = 16
            """
        ).fetchone()

    assert privileges == (
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    )
    assert saved_audit_function == (
        "campus_ai_audit_definer",
        True,
        ["search_path=pg_catalog"],
        False,
        False,
        False,
        False,
    )


def test_bootstrap_repeat_removes_runtime_and_public_retired_repair_acl_drift(
    current_master_data_schema: PostgresRoles,
) -> None:
    def retired_acl_catalog() -> set[tuple[object, ...]]:
        with current_master_data_schema.admin_connection() as connection:
            rows = connection.execute(
                """
                SELECT COALESCE(grantee.rolname, 'PUBLIC'),
                       'table', relation.relname, NULL::text, acl.privilege_type
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                CROSS JOIN LATERAL aclexplode(relation.relacl) AS acl
                LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE namespace.nspname = 'public'
                  AND relation.relname = ANY(%s)
                  AND (acl.grantee = 0 OR grantee.rolname = 'campus_ai_runtime')
                UNION ALL
                SELECT COALESCE(grantee.rolname, 'PUBLIC'),
                       'column', relation.relname, attribute.attname, acl.privilege_type
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                JOIN pg_attribute AS attribute ON attribute.attrelid = relation.oid
                CROSS JOIN LATERAL aclexplode(attribute.attacl) AS acl
                LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE namespace.nspname = 'public'
                  AND relation.relname = ANY(%s)
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
                  AND (acl.grantee = 0 OR grantee.rolname = 'campus_ai_runtime')
                ORDER BY 1, 2, 3, 4 NULLS FIRST, 5
                """,
                (list(_RETIRED_REPAIR_TABLES), list(_RETIRED_REPAIR_TABLES)),
            ).fetchall()
        return set(rows)

    def retired_policy_catalog() -> list[tuple[object, ...]]:
        with current_master_data_schema.admin_connection() as connection:
            return connection.execute(
                """
                SELECT tablename, policyname, cmd
                FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename = ANY(%s)
                ORDER BY tablename, policyname
                """,
                (list(_RETIRED_REPAIR_TABLES),),
            ).fetchall()

    try:
        with current_master_data_schema.admin_connection() as connection:
            connection.execute(
                "DROP POLICY IF EXISTS sf5b_rogue_retired_allow ON public.responsibility_types"
            )
            connection.execute(
                "CREATE POLICY sf5b_rogue_retired_allow "
                "ON public.responsibility_types AS PERMISSIVE FOR SELECT "
                "TO campus_ai_runtime USING (true)"
            )
            for table_name in _RETIRED_REPAIR_TABLES:
                identifier = sql.Identifier(table_name)
                connection.execute(
                    sql.SQL("GRANT SELECT ON TABLE public.{} TO campus_ai_runtime").format(
                        identifier
                    )
                )
                connection.execute(
                    sql.SQL("GRANT UPDATE (id) ON TABLE public.{} TO campus_ai_runtime").format(
                        identifier
                    )
                )
                connection.execute(
                    sql.SQL("GRANT SELECT (id) ON TABLE public.{} TO PUBLIC").format(identifier)
                )
        assert retired_acl_catalog()
        assert (
            "responsibility_types",
            "sf5b_rogue_retired_allow",
            "SELECT",
        ) in retired_policy_catalog()

        assert current_master_data_schema.rerun_bootstrap().returncode == 0
        assert current_master_data_schema.rerun_bootstrap().returncode == 0
        assert retired_acl_catalog() == set()
        assert retired_policy_catalog() == [
            (table_name, "sf5b_anchor_guard", "ALL") for table_name in _RETIRED_REPAIR_TABLES
        ]
    finally:
        with current_master_data_schema.admin_connection() as connection:
            connection.execute(
                "DROP POLICY IF EXISTS sf5b_rogue_retired_allow ON public.responsibility_types"
            )
            for table_name in _RETIRED_REPAIR_TABLES:
                identifier = sql.Identifier(table_name)
                connection.execute(
                    sql.SQL(
                        "REVOKE ALL PRIVILEGES ON TABLE public.{} "
                        "FROM campus_ai_runtime, PUBLIC CASCADE"
                    ).format(identifier)
                )
                connection.execute(
                    sql.SQL(
                        "REVOKE UPDATE (id) ON TABLE public.{} FROM campus_ai_runtime CASCADE"
                    ).format(identifier)
                )
                connection.execute(
                    sql.SQL("REVOKE SELECT (id) ON TABLE public.{} FROM PUBLIC CASCADE").format(
                        identifier
                    )
                )
        current_master_data_schema.rerun_bootstrap()


def test_runtime_cannot_read_or_write_retired_repair_tables(
    current_master_data_schema: PostgresRoles,
) -> None:
    facts = {
        "school_id": uuid4(),
        "person_id": uuid4(),
        "assignment_id": uuid4(),
    }
    with psycopg.connect(
        **current_master_data_schema.runtime_connection_parameters,
        autocommit=False,
    ) as runtime:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime.execute("SELECT count(*) FROM repair_scope_bindings").fetchone()
        runtime.rollback()

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with runtime.transaction():
                _insert_unresolved_ticket(runtime, facts)
