from __future__ import annotations

import pytest

from tests.master_data.conftest import PostgresRoles


@pytest.mark.integration
def test_identity_authorization_audit_schema_is_created_by_migration(
    current_master_data_schema: PostgresRoles,
) -> None:
    """共享迁移 head 必须保留旧身份、授权和不可变审计结构。"""

    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        table_names = {
            row[0]
            for row in connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                """
            ).fetchall()
        }
        user_unique_columns = {
            tuple(row[0])
            for row in connection.execute(
                """
                SELECT array_agg(attribute.attname ORDER BY key.ordinality)
                FROM pg_constraint AS constraint_record
                JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                CROSS JOIN LATERAL unnest(constraint_record.conkey)
                    WITH ORDINALITY AS key(attribute_number, ordinality)
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = relation.oid
                 AND attribute.attnum = key.attribute_number
                WHERE namespace.nspname = 'public'
                  AND relation.relname = 'users'
                  AND constraint_record.contype = 'u'
                GROUP BY constraint_record.oid
                """
            ).fetchall()
        }
        audit_actor_foreign_key = connection.execute(
            """
            SELECT constraint_record.confdeltype
            FROM pg_constraint AS constraint_record
            JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            JOIN pg_attribute AS attribute
              ON attribute.attrelid = relation.oid
             AND attribute.attnum = ANY(constraint_record.conkey)
            WHERE namespace.nspname = 'public'
              AND relation.relname = 'audit_logs'
              AND constraint_record.contype = 'f'
              AND attribute.attname = 'actor_user_id'
            """
        ).fetchone()
        audit_columns = {
            row[0]
            for row in connection.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'audit_logs'
                """
            ).fetchall()
        }
        actual_audit_indexes = {
            row[0]: list(row[1])
            for row in connection.execute(
                """
                SELECT index_relation.relname,
                       array_agg(attribute.attname ORDER BY key.ordinality)
                FROM pg_index AS index_record
                JOIN pg_class AS table_relation ON table_relation.oid = index_record.indrelid
                JOIN pg_namespace AS namespace ON namespace.oid = table_relation.relnamespace
                JOIN pg_class AS index_relation ON index_relation.oid = index_record.indexrelid
                CROSS JOIN LATERAL unnest(index_record.indkey)
                    WITH ORDINALITY AS key(attribute_number, ordinality)
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = table_relation.oid
                 AND attribute.attnum = key.attribute_number
                WHERE namespace.nspname = 'public'
                  AND table_relation.relname = 'audit_logs'
                GROUP BY index_relation.relname
                """
            ).fetchall()
        }
        trigger_names = {
            row[0]
            for row in connection.execute(
                """
                SELECT tgname
                FROM pg_trigger
                WHERE tgrelid = 'public.audit_logs'::regclass
                  AND NOT tgisinternal
                """
            ).fetchall()
        }

    assert {
        "users",
        "roles",
        "user_roles",
        "role_permissions",
        "responsibility_scopes",
        "audit_logs",
    } <= table_names
    assert ("provider_code", "provider_tenant_key", "external_subject") in user_unique_columns
    assert ("external_subject",) not in user_unique_columns
    assert audit_actor_foreign_key == ("r",)
    assert {
        "actor_user_id",
        "event_type",
        "target_type",
        "target_id",
        "before_summary",
        "after_summary",
        "request_id",
        "reason",
        "created_at",
    } <= audit_columns
    expected_audit_indexes = {
        "ix_audit_logs_created_at": ["created_at"],
        "ix_audit_logs_actor_created_at": ["actor_user_id", "created_at"],
        "ix_audit_logs_target_created_at": ["target_type", "target_id", "created_at"],
        "ix_audit_logs_request_id": ["request_id"],
    }
    assert {
        index_name: actual_audit_indexes[index_name] for index_name in expected_audit_indexes
    } == expected_audit_indexes
    assert "trg_audit_logs_reject_update_delete" in trigger_names
