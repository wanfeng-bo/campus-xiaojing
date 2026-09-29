"""Retired direct-DB import tests remain historical evidence, not current contracts."""

from __future__ import annotations

import importlib
import tomllib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event, local
from time import monotonic
from types import ModuleType
from typing import Any, cast, get_args
from uuid import UUID, uuid4, uuid5
from zipfile import ZIP_DEFLATED, ZipFile

import psycopg
import pytest
from openpyxl import Workbook
from psycopg import sql
from pydantic import ValidationError
from sqlalchemy import URL, bindparam, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.settings import Settings
from app.db.rls_context import DatabaseExecutionContextEnvelope, set_local_execution_context
from app.db.uow import CommitOutcomeUnknown
from app.platform.contracts import (
    AssignmentId,
    CommandExecutionContext,
    GuardianRelationshipId,
    PersonId,
    SchoolId,
    StaffActorContext,
    _issue_guardian_actor_context,  # pyright: ignore[reportPrivateUsage]
    _issue_staff_actor_context,  # pyright: ignore[reportPrivateUsage]
)
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import compose_command_context
from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from tests.master_data.conftest import PostgresRoles
from tests.master_data.test_execution_context import (
    _seed_formal_structure_lineage,  # pyright: ignore[reportPrivateUsage]
)

_CONTEXT_KEY = b"master-data-schema-context-signing-key-32-bytes"
_SESSION_NONCE_HASH = "8" * 64
application = importlib.import_module("app.platform.master_data_imports.application")


@dataclass(frozen=True, slots=True)
class ImportActorFacts:
    school_id: UUID
    actor_user_id: int
    actor_person_id: UUID
    actor_assignment_id: UUID


@dataclass(frozen=True, slots=True)
class ImportWriteSnapshot:
    """以脱敏逐字段投影冻结一个学校的完整导入写集合。"""

    batch_rows: tuple[tuple[object, ...], ...] = field(repr=False)
    unit_rows: tuple[tuple[object, ...], ...] = field(repr=False)
    person_rows: tuple[tuple[object, ...], ...] = field(repr=False)
    name_rows: tuple[tuple[object, ...], ...] = field(repr=False)
    idempotency_rows: tuple[tuple[object, ...], ...] = field(repr=False)
    outcome_rows: tuple[tuple[object, ...], ...] = field(repr=False)
    takeover_rows: tuple[tuple[object, ...], ...] = field(repr=False)
    audit_rows: tuple[tuple[object, ...], ...] = field(repr=False)
    outbox_rows: tuple[tuple[object, ...], ...] = field(repr=False)
    calendar_version_rows: tuple[tuple[object, ...], ...] = field(repr=False)
    calendar_day_rows: tuple[tuple[object, ...], ...] = field(repr=False)


def _full_import_write_snapshot(
    roles: PostgresRoles,
    school_id: UUID,
) -> ImportWriteSnapshot:
    """每次用新的 Owner 只读事务读取显式列；自由文本只进入固定摘要。"""

    queries = (
        """
        SELECT id, school_id, mode, source_kind, md5(source_ref), source_sha256,
               md5(COALESCE(filename_summary, '<null>')), source_size_bytes,
               md5(worksheet_names::text), mapping_version,
               md5(COALESCE(mapping_payload::text, '<null>')), status,
               planned_units, succeeded_units, failed_units, pending_units,
               confirmation_card_sha256, confirmed_by_user_id, confirmed_by_person_id,
               confirmed_by_assignment_id, confirmed_at, request_id,
               idempotency_key_hash, active_idempotency_record_id, active_action_code,
               version, created_at, updated_at
        FROM master_data_import_batches WHERE school_id = %s ORDER BY id
        """,
        """
        SELECT id, school_id, batch_id, sequence, row_number, unit_kind, status,
               md5(payload::text), payload_sha256, md5(soft_anomalies::text),
               md5(initial_soft_anomalies::text), md5(COALESCE(decision_reason, '<null>')),
               target_id, target_version, error_code, error_field_code,
               last_attempt_idempotency_record_id, last_attempt_completed,
               version, created_at, updated_at
        FROM master_data_import_units WHERE school_id = %s ORDER BY id
        """,
        """
        SELECT id, school_id, status, version, created_at, updated_at
        FROM school_people WHERE school_id = %s ORDER BY id
        """,
        """
        SELECT id, school_id, person_id, md5(display_name), valid_from, valid_to,
               source_kind, md5(source_ref), status, version, created_at, updated_at
        FROM school_person_names WHERE school_id = %s ORDER BY id
        """,
        """
        SELECT id, school_id, action_code, actor_user_id, actor_person_id,
               actor_context_type, actor_context_key, actor_assignment_id,
               actor_assignment_version, actor_guardian_relationship_id,
               actor_guardian_relationship_version, resource_type, resource_id,
               idempotency_key_hash, request_sha256, status, result_type,
               md5(COALESCE(result_payload::text, '<null>')), created_at, updated_at
        FROM command_idempotency_records WHERE school_id = %s ORDER BY id
        """,
        """
        SELECT id, school_id, idempotency_record_id, batch_id, action_code, status,
               batch_version, succeeded_units, failed_units, executed_units,
               md5(COALESCE(unit_outcome_snapshots::text, '<null>')), created_at
        FROM master_data_import_command_outcomes WHERE school_id = %s ORDER BY id
        """,
        """
        SELECT id, school_id, batch_id, previous_idempotency_record_id,
               new_idempotency_record_id, original_actor_user_id,
               original_actor_person_id, original_actor_assignment_id,
               original_actor_assignment_version, new_actor_user_id,
               new_actor_person_id, new_actor_assignment_id,
               new_actor_assignment_version, md5(takeover_reason), request_id,
               batch_version_before, batch_version_after, transferred_sequences,
               transferred_unit_versions, created_at
        FROM master_data_import_takeovers WHERE school_id = %s ORDER BY id
        """,
        """
        SELECT id, actor_user_id, school_id, actor_person_id, actor_assignment_id,
               actor_assignment_version, actor_guardian_relationship_id,
               actor_guardian_relationship_version, actor_context_type,
               idempotency_key_hash, event_type, target_type, target_id,
               md5(before_summary::text), md5(after_summary::text), request_id,
               md5(reason), created_at
        FROM audit_logs WHERE school_id = %s ORDER BY id
        """,
        """
        SELECT id, school_id, md5(event_key), event_type, aggregate_type,
               aggregate_id, aggregate_version, occurred_at, md5(payload::text), status,
               attempt_count, available_at, created_at, updated_at
        FROM outbox_messages WHERE school_id = %s ORDER BY id
        """,
        """
        SELECT id, school_id, md5(display_name), timezone_name, valid_from, valid_to,
               source_kind, md5(source_ref), status, version, created_at, updated_at
        FROM school_calendar_versions WHERE school_id = %s ORDER BY id
        """,
        """
        SELECT id, school_id, calendar_version_id, local_date, day_kind,
               actual_start_time, actual_end_time, source_kind,
               md5(COALESCE(note, '<null>')), version, created_at, updated_at
        FROM school_calendar_days WHERE school_id = %s ORDER BY id
        """,
    )
    with roles.migration_connection() as connection, connection.transaction():
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        connection.execute("SET TRANSACTION READ ONLY")
        rows = tuple(
            tuple(tuple(row) for row in connection.execute(query, (school_id,)).fetchall())
            for query in queries
        )
    return ImportWriteSnapshot(*rows)


def _dual_import_write_snapshot(
    roles: PostgresRoles,
    first_school_id: UUID,
    second_school_id: UUID,
) -> tuple[ImportWriteSnapshot, ImportWriteSnapshot]:
    """分别冻结两个学校，避免跨校拒绝只证明一侧没有变化。"""

    return (
        _full_import_write_snapshot(roles, first_school_id),
        _full_import_write_snapshot(roles, second_school_id),
    )


@dataclass(frozen=True, slots=True)
class TerminalImportFacts:
    """保留终态导入及其可信 runtime 上下文，供数据库旁路负测复用。"""

    roles: PostgresRoles
    uow_factory: Any
    context: CommandExecutionContext
    school_id: UUID
    batch_id: UUID

    def snapshot(self) -> tuple[Any, ...]:
        """从新 Owner 连接读取当前批次及同校完整写集合摘要。"""

        with self.roles.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            row = connection.execute(
                """
                SELECT
                    (SELECT to_jsonb(batch_row)
                     FROM master_data_import_batches AS batch_row
                     WHERE batch_row.school_id = %s AND batch_row.id = %s),
                    (SELECT COALESCE(
                        jsonb_agg(to_jsonb(unit_row) ORDER BY unit_row.id),
                        '[]'::jsonb
                     )
                     FROM master_data_import_units AS unit_row
                     WHERE unit_row.school_id = %s AND unit_row.batch_id = %s),
                    (SELECT COALESCE(
                        jsonb_agg(to_jsonb(person_row) ORDER BY person_row.id),
                        '[]'::jsonb
                     )
                     FROM school_people AS person_row
                     WHERE person_row.school_id = %s),
                    (SELECT COALESCE(
                        jsonb_agg(to_jsonb(name_row) ORDER BY name_row.id),
                        '[]'::jsonb
                     )
                     FROM school_person_names AS name_row
                     WHERE name_row.school_id = %s),
                    (SELECT COALESCE(
                        jsonb_agg(to_jsonb(record_row) ORDER BY record_row.id),
                        '[]'::jsonb
                     )
                     FROM command_idempotency_records AS record_row
                     WHERE record_row.school_id = %s),
                    (SELECT COALESCE(
                        jsonb_agg(to_jsonb(outcome_row) ORDER BY outcome_row.id),
                        '[]'::jsonb
                     )
                     FROM master_data_import_command_outcomes AS outcome_row
                     WHERE outcome_row.school_id = %s),
                    (SELECT COALESCE(
                        jsonb_agg(to_jsonb(takeover_row) ORDER BY takeover_row.id),
                        '[]'::jsonb
                     )
                     FROM master_data_import_takeovers AS takeover_row
                     WHERE takeover_row.school_id = %s),
                    (SELECT COALESCE(
                        jsonb_agg(to_jsonb(audit_row) ORDER BY audit_row.id),
                        '[]'::jsonb
                     )
                     FROM audit_logs AS audit_row
                     WHERE audit_row.school_id = %s),
                    (SELECT COALESCE(
                        jsonb_agg(to_jsonb(outbox_row) ORDER BY outbox_row.id),
                        '[]'::jsonb
                     )
                     FROM outbox_messages AS outbox_row
                     WHERE outbox_row.school_id = %s)
                """,
                (
                    self.school_id,
                    self.batch_id,
                    self.school_id,
                    self.batch_id,
                    self.school_id,
                    self.school_id,
                    self.school_id,
                    self.school_id,
                    self.school_id,
                    self.school_id,
                    self.school_id,
                ),
            ).fetchone()
        assert row is not None
        return tuple(row)


class _StopAfterActiveCommandCommit(RuntimeError):
    """测试专用停止点：正式 confirm 已提交活动命令，但尚未执行首个单元。"""


@dataclass(frozen=True, slots=True)
class ApplyingImportScenario:
    """由正式 stage/preflight/confirm 路径形成的未完成普通导入场景。"""

    roles: PostgresRoles
    session_factory: sessionmaker[Session]
    school_id: UUID
    batch_id: UUID
    original_assignment_id: UUID
    original_confirmer: tuple[Any, ...]
    old_idempotency_record_id: UUID
    takeover_command: Any
    replacement_service: Any
    replacement_context: CommandExecutionContext
    cross_school_id: UUID
    cross_school_batch_id: UUID

    def snapshot(self) -> tuple[Any, ...]:
        """读取当前学校全部接管相关事实，供拒绝路径证明零写入。"""

        return self._snapshot(all_schools=False)

    def snapshot_all_schools(self) -> tuple[Any, ...]:
        """读取所有学校的接管相关事实，防止跨校拒绝隐藏旁路写入。"""

        return self._snapshot(all_schools=True)

    def _snapshot(self, *, all_schools: bool) -> tuple[Any, ...]:
        school_filter = "" if all_schools else "WHERE school_id = %s"
        parameters: tuple[object, ...] = () if all_schools else (self.school_id,) * 9
        with self.roles.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            row = connection.execute(
                f"""
                SELECT
                    (SELECT COALESCE(jsonb_agg(to_jsonb(item) ORDER BY item.id), '[]'::jsonb)
                     FROM master_data_import_batches AS item {school_filter}),
                    (SELECT COALESCE(jsonb_agg(to_jsonb(item) ORDER BY item.id), '[]'::jsonb)
                     FROM master_data_import_units AS item {school_filter}),
                    (SELECT COALESCE(jsonb_agg(to_jsonb(item) ORDER BY item.id), '[]'::jsonb)
                     FROM school_people AS item {school_filter}),
                    (SELECT COALESCE(jsonb_agg(to_jsonb(item) ORDER BY item.id), '[]'::jsonb)
                     FROM school_person_names AS item {school_filter}),
                    (SELECT COALESCE(jsonb_agg(to_jsonb(item) ORDER BY item.id), '[]'::jsonb)
                     FROM command_idempotency_records AS item {school_filter}),
                    (SELECT COALESCE(jsonb_agg(to_jsonb(item) ORDER BY item.id), '[]'::jsonb)
                     FROM master_data_import_command_outcomes AS item {school_filter}),
                    (SELECT COALESCE(jsonb_agg(to_jsonb(item) ORDER BY item.id), '[]'::jsonb)
                     FROM master_data_import_takeovers AS item {school_filter}),
                    (SELECT COALESCE(jsonb_agg(to_jsonb(item) ORDER BY item.id), '[]'::jsonb)
                     FROM audit_logs AS item {school_filter}),
                    (SELECT COALESCE(jsonb_agg(to_jsonb(item) ORDER BY item.id), '[]'::jsonb)
                     FROM outbox_messages AS item {school_filter})
                """,
                parameters,
            ).fetchone()
        assert row is not None
        return tuple(row)

    def revoke_original_assignment(self) -> None:
        """由 Owner 撤销原任职并提升版本，模拟活动命令失去履职上下文。"""

        with self.roles.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            revoked_content = connection.execute(
                """
                UPDATE staff_assignment_versions
                SET revoked_at = clock_timestamp()
                WHERE school_id = %s AND assignment_id = %s AND revoked_at IS NULL
                """,
                (self.school_id, self.original_assignment_id),
            ).rowcount
            changed = connection.execute(
                """
                UPDATE staff_assignments
                SET status = 'rejected', version = version + 1
                WHERE school_id = %s AND id = %s
                """,
                (self.school_id, self.original_assignment_id),
            ).rowcount
        assert revoked_content >= 1
        assert changed == 1

    def invalidate_original_assignment(self, invalidity: str) -> None:
        """只改变一个权威任职事实，独立证明各失效分支。"""

        replacement_actor = self.replacement_context.actor
        assert type(replacement_actor) is StaffActorContext
        mutations = {
            "person": (
                sql.SQL("person_id = %s"),
                (replacement_actor.person_id,),
                sql.SQL("person_id = %s"),
                (replacement_actor.person_id,),
            ),
            "version": (sql.SQL("version = version + 1"), (), None, ()),
            "status": (
                sql.SQL("status = 'rejected'"),
                (),
                sql.SQL("status = 'rejected'"),
                (),
            ),
            "valid_from": (
                sql.SQL("valid_from = clock_timestamp() + interval '1 day'"),
                (),
                sql.SQL("effective_from = clock_timestamp() + interval '1 day'"),
                (),
            ),
            "valid_to": (
                sql.SQL("valid_to = clock_timestamp() - interval '1 second'"),
                (),
                sql.SQL("planned_valid_to = clock_timestamp() - interval '1 second'"),
                (),
            ),
        }
        mutation_sql, mutation_parameters, content_sql, content_parameters = mutations[invalidity]
        with self.roles.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            changed = connection.execute(
                sql.SQL(
                    """
                UPDATE staff_assignments
                SET {}
                WHERE school_id = %s AND id = %s
                """
                ).format(mutation_sql),
                (*mutation_parameters, self.school_id, self.original_assignment_id),
            ).rowcount
            content_changed = 0
            if content_sql is not None:
                content_changed = connection.execute(
                    sql.SQL(
                        "UPDATE staff_assignment_versions SET {} "
                        "WHERE school_id = %s AND assignment_id = %s"
                    ).format(content_sql),
                    (*content_parameters, self.school_id, self.original_assignment_id),
                ).rowcount
        assert changed == 1
        if content_sql is not None:
            assert content_changed >= 1

    def set_original_availability(self, availability: str) -> None:
        """记录测试意图但不伪造任职失效；可用性文字没有任职权威语义。"""

        assert availability in {"confirmed", "busy", "business_trip", "ordinary_leave"}
        with self.roles.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            row = connection.execute(
                """
                SELECT status, version, valid_from < clock_timestamp(),
                       valid_to IS NULL OR valid_to > clock_timestamp()
                FROM staff_assignments
                WHERE school_id = %s AND id = %s
                """,
                (self.school_id, self.original_assignment_id),
            ).fetchone()
        assert row == ("confirmed", 1, True, True)

    def command_with_takeover_reason(self, reason: str) -> Any:
        """仅替换自由文本说明，不向命令注入任何权威事实。"""

        return self.takeover_command.model_copy(update={"takeover_reason": reason})

    def command_with_mutation(self, mutation: str) -> Any:
        """逐项构造卡片、批次、单元版本或跨校目标变化。"""

        if mutation == "confirmation_card":
            return self.takeover_command.model_copy(update={"confirmation_card_sha256": "0" * 64})
        if mutation == "batch_version":
            return self.takeover_command.model_copy(
                update={"expected_batch_version": self.takeover_command.expected_batch_version + 1}
            )
        if mutation == "unit_version":
            changed_unit = self.takeover_command.units[0].model_copy(
                update={
                    "expected_unit_version": (
                        self.takeover_command.units[0].expected_unit_version + 1
                    )
                }
            )
            return self.takeover_command.model_copy(update={"units": (changed_unit,)})
        if mutation == "cross_school_batch":
            return self.takeover_command.model_copy(update={"batch_id": self.cross_school_batch_id})
        raise AssertionError(f"未知接管变化：{mutation}")

    def read_original_confirmer(self) -> tuple[Any, ...]:
        """读取首次确认归属，接管不得改写。"""

        with self.roles.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            row = connection.execute(
                """
                SELECT confirmed_by_user_id, confirmed_by_person_id,
                       confirmed_by_assignment_id, confirmed_at
                FROM master_data_import_batches
                WHERE school_id = %s AND id = %s
                """,
                (self.school_id, self.batch_id),
            ).fetchone()
        assert row is not None
        return tuple(row)

    def read_takeover_count(self) -> int:
        """读取当前批次的不可变接管记录数。"""

        with self.roles.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            row = connection.execute(
                """
                SELECT count(*) FROM master_data_import_takeovers
                WHERE school_id = %s AND batch_id = %s
                """,
                (self.school_id, self.batch_id),
            ).fetchone()
        assert row is not None
        return int(row[0])


@pytest.fixture(name="import_runtime_session_factory")
def _import_runtime_session_factory(
    current_master_data_schema: PostgresRoles,
) -> Iterator[sessionmaker[Session]]:
    runtime = current_master_data_schema.runtime_connection_parameters
    engine = create_engine(
        URL.create(
            drivername="postgresql+psycopg",
            username=str(runtime["user"]),
            password=str(runtime["password"]),
            host=str(runtime["host"]),
            port=int(runtime["port"]),
            database=str(runtime["dbname"]),
        ),
        poolclass=NullPool,
    )
    try:
        yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    finally:
        engine.dispose()


def _seed_import_actor(current_master_data_schema: PostgresRoles) -> ImportActorFacts:
    marker = uuid4().hex
    school_id = uuid4()
    actor_person_id = uuid4()
    actor_unit_id = uuid4()
    actor_assignment_id = uuid4()
    actor_binding_id = uuid4()
    fact_source_id = uuid4()
    fact_source_key = f"task8-content:{marker}"
    valid_from = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
    with psycopg.connect(
        **current_master_data_schema.migration_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO schools (
                id, school_code, display_name, timezone_name, status, version
            ) VALUES (%s, %s, 'Task 8 学校', 'Asia/Shanghai', 'active', 1)
            """,
            (school_id, f"task8-{marker}"),
        )
        connection.execute(
            """
            INSERT INTO school_fact_sources (
                id, school_id, source_key, source_kind, source_ref, schema_version
            ) VALUES (%s, %s, %s, 'legacy', %s, 1)
            """,
            (
                fact_source_id,
                school_id,
                fact_source_key,
                fact_source_key,
            ),
        )
        user_row = connection.execute(
            """
            INSERT INTO users (external_subject, display_name, is_active, security_version)
            VALUES (%s, 'Task 8 操作者', true, 1)
            RETURNING id, provider_code, provider_tenant_key, external_subject
            """,
            (f"task8:{marker}",),
        ).fetchone()
        assert user_row is not None
        actor_user_id = int(user_row[0])
        connection.execute(
            """
            INSERT INTO school_people (id, school_id, status, version)
            VALUES (%s, %s, 'active', 1)
            """,
            (actor_person_id, school_id),
        )
        connection.execute(
            """
            INSERT INTO account_person_bindings (
                id, school_id, account_user_id, person_id,
                source_key, valid_from, status, version
            ) VALUES (%s, %s, %s, %s, %s, %s, 'confirmed', 1)
            """,
            (
                actor_binding_id,
                school_id,
                actor_user_id,
                actor_person_id,
                fact_source_key,
                valid_from,
            ),
        )
        connection.execute(
            """
            INSERT INTO account_person_binding_versions (
                id, school_id, binding_id, account_user_id, person_id,
                provider_code, provider_tenant_key, external_subject,
                effective_from, effective_to, revoked_at, status, target_version,
                source_ref, source_id, import_batch_id
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, NULL, NULL, 'confirmed', 1,
                %s, %s, NULL
            )
            """,
            (
                uuid4(),
                school_id,
                actor_binding_id,
                actor_user_id,
                actor_person_id,
                str(user_row[1]),
                str(user_row[2]),
                str(user_row[3]),
                valid_from,
                fact_source_key,
                fact_source_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO organization_units (id, school_id, kind, status, version)
            VALUES (%s, %s, 'administration', 'active', 1)
            """,
            (actor_unit_id, school_id),
        )
        connection.execute(
            """
            INSERT INTO staff_assignments (
                id, school_id, person_id, organization_unit_id, position_title,
                semantic_role_code, appointment_source, approval_reference,
                source_key, valid_from, status, version
            ) VALUES (
                %s, %s, %s, %s, '主数据管理员', 'school_master_data_manager',
                'manual', %s, %s, %s, 'confirmed', 1
            )
            """,
            (
                actor_assignment_id,
                school_id,
                actor_person_id,
                actor_unit_id,
                f"task8-actor-{marker}",
                fact_source_key,
                valid_from,
            ),
        )
        connection.execute(
            """
            INSERT INTO staff_assignment_versions (
                id, school_id, assignment_id, person_id, organization_unit_id,
                position_title, semantic_role_code, assignment_kind,
                appointment_source, approval_reference,
                effective_from, effective_to, planned_valid_to, revoked_at,
                status, target_version, source_ref, source_id, import_batch_id
            ) VALUES (
                %s, %s, %s, %s, %s,
                '主数据管理员', 'school_master_data_manager', 'substantive',
                'manual', %s,
                %s, NULL, NULL, NULL,
                'confirmed', 1, %s, %s, NULL
            )
            """,
            (
                uuid4(),
                school_id,
                actor_assignment_id,
                actor_person_id,
                actor_unit_id,
                f"task8-actor-{marker}",
                valid_from,
                fact_source_key,
                fact_source_id,
            ),
        )
        connection.commit()
    return ImportActorFacts(
        school_id=school_id,
        actor_user_id=actor_user_id,
        actor_person_id=actor_person_id,
        actor_assignment_id=actor_assignment_id,
    )


def _seed_same_school_import_actor(
    current_master_data_schema: PostgresRoles,
    *,
    school_id: UUID,
) -> ImportActorFacts:
    """在既有测试学校建立另一名有效主数据管理员。"""

    marker = uuid4().hex
    actor_person_id = uuid4()
    actor_unit_id = uuid4()
    actor_assignment_id = uuid4()
    actor_binding_id = uuid4()
    fact_source_id = uuid4()
    fact_source_key = f"task8-same-school-content:{marker}"
    valid_from = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO school_fact_sources (
                id, school_id, source_key, source_kind, source_ref, schema_version
            ) VALUES (%s, %s, %s, 'legacy', %s, 1)
            """,
            (
                fact_source_id,
                school_id,
                fact_source_key,
                fact_source_key,
            ),
        )
        user_row = connection.execute(
            """
            INSERT INTO users (external_subject, display_name, is_active, security_version)
            VALUES (%s, 'Task 8 同校操作者', true, 1)
            RETURNING id, provider_code, provider_tenant_key, external_subject
            """,
            (f"task8-same-school:{marker}",),
        ).fetchone()
        assert user_row is not None
        actor_user_id = int(user_row[0])
        connection.execute(
            "INSERT INTO school_people (id, school_id, status, version) "
            "VALUES (%s, %s, 'active', 1)",
            (actor_person_id, school_id),
        )
        connection.execute(
            """
            INSERT INTO account_person_bindings (
                id, school_id, account_user_id, person_id,
                source_key, valid_from, status, version
            ) VALUES (%s, %s, %s, %s, %s, %s, 'confirmed', 1)
            """,
            (
                actor_binding_id,
                school_id,
                actor_user_id,
                actor_person_id,
                fact_source_key,
                valid_from,
            ),
        )
        connection.execute(
            """
            INSERT INTO account_person_binding_versions (
                id, school_id, binding_id, account_user_id, person_id,
                provider_code, provider_tenant_key, external_subject,
                effective_from, effective_to, revoked_at, status, target_version,
                source_ref, source_id, import_batch_id
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, NULL, NULL, 'confirmed', 1,
                %s, %s, NULL
            )
            """,
            (
                uuid4(),
                school_id,
                actor_binding_id,
                actor_user_id,
                actor_person_id,
                str(user_row[1]),
                str(user_row[2]),
                str(user_row[3]),
                valid_from,
                fact_source_key,
                fact_source_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO organization_units (id, school_id, kind, status, version)
            VALUES (%s, %s, 'administration', 'active', 1)
            """,
            (actor_unit_id, school_id),
        )
        connection.execute(
            """
            INSERT INTO staff_assignments (
                id, school_id, person_id, organization_unit_id, position_title,
                semantic_role_code, appointment_source, approval_reference,
                source_key, valid_from, status, version
            ) VALUES (
                %s, %s, %s, %s, '同校主数据管理员', 'school_master_data_manager',
                'manual', %s, %s, %s, 'confirmed', 1
            )
            """,
            (
                actor_assignment_id,
                school_id,
                actor_person_id,
                actor_unit_id,
                f"task8-same-school-{marker}",
                fact_source_key,
                valid_from,
            ),
        )
        connection.execute(
            """
            INSERT INTO staff_assignment_versions (
                id, school_id, assignment_id, person_id, organization_unit_id,
                position_title, semantic_role_code, assignment_kind,
                appointment_source, approval_reference,
                effective_from, effective_to, planned_valid_to, revoked_at,
                status, target_version, source_ref, source_id, import_batch_id
            ) VALUES (
                %s, %s, %s, %s, %s,
                '同校主数据管理员', 'school_master_data_manager', 'substantive',
                'manual', %s,
                %s, NULL, NULL, NULL,
                'confirmed', 1, %s, %s, NULL
            )
            """,
            (
                uuid4(),
                school_id,
                actor_assignment_id,
                actor_person_id,
                actor_unit_id,
                f"task8-same-school-{marker}",
                valid_from,
                fact_source_key,
                fact_source_id,
            ),
        )
        connection.commit()
    return ImportActorFacts(
        school_id=school_id,
        actor_user_id=actor_user_id,
        actor_person_id=actor_person_id,
        actor_assignment_id=actor_assignment_id,
    )


def _seed_confirmed_person_name(
    current_master_data_schema: PostgresRoles,
    *,
    school_id: UUID,
    display_name: str,
    external_reference: str,
) -> UUID:
    """用 Owner 在隔离测试学校写入一条已确认姓名事实。"""

    person_id = uuid4()
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "INSERT INTO school_people (id, school_id, status, version) "
            "VALUES (%s, %s, 'active', 1)",
            (person_id, school_id),
        )
        connection.execute(
            """
            INSERT INTO school_person_names (
                id, school_id, person_id, display_name, valid_from,
                source_kind, source_ref, status, version
            ) VALUES (%s, %s, %s, %s, %s,
                      'person_external_reference', %s, 'confirmed', 1)
            """,
            (
                uuid4(),
                school_id,
                person_id,
                display_name,
                datetime.now(UTC).replace(microsecond=0) - timedelta(days=1),
                external_reference,
            ),
        )
        connection.commit()
    return person_id


@pytest.fixture(name="import_actor_facts")
def _import_actor_facts(current_master_data_schema: PostgresRoles) -> ImportActorFacts:
    return _seed_import_actor(current_master_data_schema)


@pytest.fixture(name="other_import_actor_facts")
def _other_import_actor_facts(current_master_data_schema: PostgresRoles) -> ImportActorFacts:
    return _seed_import_actor(current_master_data_schema)


def _command_context(
    facts: ImportActorFacts,
    *,
    idempotency_key: str,
) -> CommandExecutionContext:
    actor = _issue_staff_actor_context(
        account_user_id=facts.actor_user_id,
        school_id=SchoolId(facts.school_id),
        person_id=PersonId(facts.actor_person_id),
        assignment_id=AssignmentId(facts.actor_assignment_id),
        assignment_version=1,
        authorization_version=1,
    )
    return compose_command_context(actor, uuid4(), idempotency_key)


def _envelope_factory(
    context: CommandExecutionContext,
) -> DatabaseExecutionContextEnvelope:
    return ContextProofSigner(_CONTEXT_KEY).sign_execution_context(
        context,
        session_nonce_hash=_SESSION_NONCE_HASH,
        proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


def _import_service_and_uow_factory(
    import_runtime_session_factory: sessionmaker[Session],
) -> tuple[Any, Any]:
    """创建使用真实 runtime Session 的导入服务和 UoW 工厂。"""

    application = importlib.import_module("app.platform.master_data_imports.application")
    uow_factory = application.SqlAlchemyMasterDataImportUoWFactory(
        session_factory=import_runtime_session_factory,
        envelope_factory=_envelope_factory,
        lock_timeout_ms=750,
    )
    return (
        application.MasterDataImportService(
            uow_factory=uow_factory,
            authorizer=application.ConfiguredMasterDataImportAuthorizer(
                frozenset({"school_master_data_manager"})
            ),
        ),
        uow_factory,
    )


def _ready_single_ordinary_confirmation(
    service: Any,
    actor: ImportActorFacts,
    *,
    marker: str,
    continue_reason: str | None = None,
) -> tuple[Any, CommandExecutionContext]:
    """准备单个普通人员单元，并返回可由同键双执行者共享的确认命令与上下文。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    content = _workbook_bytes(
        ("姓名", "编号"),
        ("同键并发教师", marker),
        title="教师名单",
    )
    staged = service.stage_ordinary(
        contracts.StageOrdinaryPersonImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"protected-same-key-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="teacher-roster.xlsx",
                size_bytes=len(content),
                worksheet_names=("教师名单",),
            ),
            worksheet_name="教师名单",
            mapping=contracts.ColumnMappingDraft(
                mapping_version=1,
                entries=(
                    contracts.ColumnMappingEntry(
                        source_column_index=1,
                        source_header="姓名",
                        target_field_code="person.display_name",
                    ),
                    contracts.ColumnMappingEntry(
                        source_column_index=2,
                        source_header="编号",
                        target_field_code="person.external_reference",
                    ),
                ),
            ),
            units=(
                contracts.PersonImportUnitDraft(
                    row_number=2,
                    display_name="同键并发教师",
                    external_reference=marker,
                    valid_from=datetime.now(UTC).replace(microsecond=0),
                ),
            ),
        ),
        context=_command_context(
            actor,
            idempotency_key=f"task8-stage-same-key-{marker}",
        ),
    )
    ready = service.preflight(
        contracts.PreflightImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            expected_mapping_version=1,
        ),
        context=_command_context(
            actor,
            idempotency_key=f"task8-preflight-same-key-{marker}",
        ),
    )
    command = contracts.ConfirmImportBatchCommand(
        batch_id=ready.batch_id,
        expected_batch_version=ready.batch_version,
        confirmation_card_sha256=ready.confirmation_card_sha256,
        decisions=(
            contracts.ImportUnitDecision(
                sequence=1,
                expected_unit_version=ready.units[0].unit_version,
                continue_reason=continue_reason,
            ),
        ),
    )
    return command, _command_context(
        actor,
        idempotency_key=f"task8-confirm-same-key-{marker}",
    )


def _ready_multi_ordinary_confirmation(
    service: Any,
    actor: ImportActorFacts,
    *,
    marker: str,
) -> tuple[Any, CommandExecutionContext]:
    """经正式 stage/preflight 生成三个普通人员单元的确认命令。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    displays = ("混合接管教师甲", "混合接管教师乙", "混合接管教师丙")
    content = _workbook_bytes(
        ("姓名", "编号"),
        *((display, f"{marker}-{index}") for index, display in enumerate(displays, 1)),
        title="教师名单",
    )
    staged = service.stage_ordinary(
        contracts.StageOrdinaryPersonImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"protected-mixed-takeover-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="mixed-takeover.xlsx",
                size_bytes=len(content),
                worksheet_names=("教师名单",),
            ),
            worksheet_name="教师名单",
            mapping=contracts.ColumnMappingDraft(
                mapping_version=1,
                entries=(
                    contracts.ColumnMappingEntry(
                        source_column_index=1,
                        source_header="姓名",
                        target_field_code="person.display_name",
                    ),
                    contracts.ColumnMappingEntry(
                        source_column_index=2,
                        source_header="编号",
                        target_field_code="person.external_reference",
                    ),
                ),
            ),
            units=tuple(
                contracts.PersonImportUnitDraft(
                    row_number=index + 1,
                    display_name=display,
                    external_reference=f"{marker}-{index}",
                    valid_from=datetime.now(UTC).replace(microsecond=0),
                )
                for index, display in enumerate(displays, 1)
            ),
        ),
        context=_command_context(
            actor,
            idempotency_key=f"task8-stage-mixed-takeover-{marker}",
        ),
    )
    ready = service.preflight(
        contracts.PreflightImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            expected_mapping_version=1,
        ),
        context=_command_context(
            actor,
            idempotency_key=f"task8-preflight-mixed-takeover-{marker}",
        ),
    )
    command = contracts.ConfirmImportBatchCommand(
        batch_id=ready.batch_id,
        expected_batch_version=ready.batch_version,
        confirmation_card_sha256=ready.confirmation_card_sha256,
        decisions=tuple(
            contracts.ImportUnitDecision(
                sequence=unit.sequence,
                expected_unit_version=unit.unit_version,
            )
            for unit in ready.units
        ),
    )
    return command, _command_context(
        actor,
        idempotency_key=f"task8-confirm-mixed-takeover-{marker}",
    )


@dataclass(frozen=True, slots=True)
class _MultiUnitTakeoverScenario:
    """停在三单元普通命令已持久化、尚未执行的接管恢复场景。"""

    roles: PostgresRoles
    service: Any
    command: Any
    context: CommandExecutionContext
    school_id: UUID
    batch_id: UUID

    def facts(self) -> tuple[Any, ...]:
        """读取批次、逐单元 attempt 和最终事实计数。"""

        with self.roles.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            row = connection.execute(
                """
                SELECT batch.status, batch.version,
                       batch.succeeded_units, batch.failed_units, batch.pending_units,
                       batch.active_idempotency_record_id, batch.active_action_code,
                       jsonb_agg(
                           jsonb_build_object(
                               'sequence', unit.sequence,
                               'status', unit.status,
                               'version', unit.version,
                               'target_id', unit.target_id,
                               'target_version', unit.target_version,
                               'error_code', unit.error_code,
                               'error_field_code', unit.error_field_code,
                               'decision_reason', unit.decision_reason,
                               'attempt_id', unit.last_attempt_idempotency_record_id,
                               'attempt_completed', unit.last_attempt_completed
                           ) ORDER BY unit.sequence
                       ),
                       (SELECT count(*) FROM school_people AS person
                        JOIN master_data_import_units AS target_unit
                          ON target_unit.school_id = person.school_id
                         AND target_unit.target_id = person.id
                        WHERE target_unit.school_id = batch.school_id
                          AND target_unit.batch_id = batch.id),
                       (SELECT count(*) FROM audit_logs AS audit
                        WHERE audit.school_id = batch.school_id
                          AND audit.event_type = 'master_data.person.imported'),
                       (SELECT count(*) FROM outbox_messages AS outbox
                        WHERE outbox.school_id = batch.school_id
                          AND outbox.event_type = 'master_data.person.imported'),
                       (SELECT count(*) FROM master_data_import_command_outcomes AS outcome
                        WHERE outcome.school_id = batch.school_id
                          AND outcome.batch_id = batch.id
                          AND outcome.action_code = 'master_data.import.ordinary_takeover')
                FROM master_data_import_batches AS batch
                JOIN master_data_import_units AS unit
                  ON unit.school_id = batch.school_id AND unit.batch_id = batch.id
                WHERE batch.school_id = %s AND batch.id = %s
                GROUP BY batch.id
                """,
                (self.school_id, self.batch_id),
            ).fetchone()
        assert row is not None
        return tuple(row)


def _prepare_multi_unit_takeover(
    monkeypatch: pytest.MonkeyPatch,
    roles: PostgresRoles,
    session_factory: sessionmaker[Session],
    actor: ImportActorFacts,
) -> _MultiUnitTakeoverScenario:
    """经正式入口形成具有三个 DB manifest 单元的接管命令。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    original_service, _ = _import_service_and_uow_factory(session_factory)
    marker = uuid4().hex
    confirm_command, confirm_context = _ready_multi_ordinary_confirmation(
        original_service,
        actor,
        marker=f"partial-takeover-{marker}",
    )

    def stop_before_first_unit(**_kwargs: Any) -> bool:
        raise _StopAfterActiveCommandCommit("三单元活动命令已提交，尚未执行首个单元")

    monkeypatch.setattr(original_service, "_apply_ordinary_person_unit", stop_before_first_unit)
    with pytest.raises(_StopAfterActiveCommandCommit):
        original_service.confirm(confirm_command, context=confirm_context)
    preview = original_service.preview(
        confirm_command.batch_id,
        context=_command_context(
            actor,
            idempotency_key=f"task8-partial-takeover-preview-{marker}",
        ),
    )
    with roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "UPDATE staff_assignments SET status = 'rejected', version = version + 1 "
            "WHERE school_id = %s AND id = %s",
            (actor.school_id, actor.actor_assignment_id),
        )
    replacement_actor = _seed_same_school_import_actor(roles, school_id=actor.school_id)
    replacement_service, _ = _import_service_and_uow_factory(session_factory)
    replacement_context = _command_context(
        replacement_actor,
        idempotency_key=f"task8-partial-takeover-replacement-{marker}",
    )
    takeover_command = contracts.TakeOverOrdinaryImportCommand(
        batch_id=preview.batch_id,
        expected_batch_version=preview.batch_version,
        confirmation_card_sha256=preview.confirmation_card_sha256,
        units=tuple(
            contracts.TakeoverImportUnitDecision(
                sequence=unit.sequence,
                expected_unit_version=unit.unit_version,
            )
            for unit in preview.units
        ),
        takeover_reason="首个处理人任职失效后继续三个未完成单元",
    )
    return _MultiUnitTakeoverScenario(
        roles=roles,
        service=replacement_service,
        command=takeover_command,
        context=replacement_context,
        school_id=actor.school_id,
        batch_id=preview.batch_id,
    )


@pytest.fixture(name="applying_import_scenario")
def _applying_import_scenario(
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    other_import_actor_facts: ImportActorFacts,
) -> ApplyingImportScenario:
    """经正式入口停在首个 active-command 提交后的普通导入场景。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    original_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    _seed_confirmed_person_name(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
        display_name="同键并发教师",
        external_reference=f"takeover-soft-anomaly-{marker}",
    )
    original_command, original_context = _ready_single_ordinary_confirmation(
        original_service,
        import_actor_facts,
        marker=f"takeover-{marker}",
        continue_reason="原处理人确认同名人员仍需继续",
    )

    def stop_before_first_unit(**_kwargs: Any) -> bool:
        raise _StopAfterActiveCommandCommit("首个 active-command COMMIT 后停止")

    monkeypatch.setattr(
        original_service,
        "_apply_ordinary_person_unit",
        stop_before_first_unit,
    )
    with pytest.raises(_StopAfterActiveCommandCommit):
        original_service.confirm(original_command, context=original_context)

    active_preview = original_service.preview(
        original_command.batch_id,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-takeover-preview-{marker}",
        ),
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        active_row = connection.execute(
            """
            SELECT status, active_idempotency_record_id,
                   confirmed_by_user_id, confirmed_by_person_id,
                   confirmed_by_assignment_id, confirmed_at
            FROM master_data_import_batches
            WHERE school_id = %s AND id = %s
            """,
            (import_actor_facts.school_id, original_command.batch_id),
        ).fetchone()
        unit_row = connection.execute(
            """
            SELECT last_attempt_idempotency_record_id, last_attempt_completed
            FROM master_data_import_units
            WHERE school_id = %s AND batch_id = %s AND sequence = 1
            """,
            (import_actor_facts.school_id, original_command.batch_id),
        ).fetchone()
    assert active_row is not None
    assert active_row[0] == "applying"
    assert isinstance(active_row[1], UUID)
    assert unit_row == (active_row[1], False)
    original_confirmer = tuple(active_row[2:])

    replacement_actor = _seed_same_school_import_actor(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
    )
    replacement_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    replacement_context = _command_context(
        replacement_actor,
        idempotency_key=f"task8-takeover-replacement-{marker}",
    )
    takeover_command = contracts.TakeOverOrdinaryImportCommand(
        batch_id=active_preview.batch_id,
        expected_batch_version=active_preview.batch_version,
        confirmation_card_sha256=active_preview.confirmation_card_sha256,
        units=tuple(
            contracts.TakeoverImportUnitDecision(
                sequence=unit.sequence,
                expected_unit_version=unit.unit_version,
                continue_reason=("接管人复核同名人员后继续" if unit.soft_anomaly_codes else None),
            )
            for unit in active_preview.units
        ),
        takeover_reason="原活动任职已撤销，由现任主数据管理员继续未完成单元",
    )

    cross_school_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    cross_school_command, _ = _ready_single_ordinary_confirmation(
        cross_school_service,
        other_import_actor_facts,
        marker=f"cross-school-takeover-{marker}",
    )
    return ApplyingImportScenario(
        roles=current_master_data_schema,
        session_factory=import_runtime_session_factory,
        school_id=import_actor_facts.school_id,
        batch_id=original_command.batch_id,
        original_assignment_id=import_actor_facts.actor_assignment_id,
        original_confirmer=original_confirmer,
        old_idempotency_record_id=active_row[1],
        takeover_command=takeover_command,
        replacement_service=replacement_service,
        replacement_context=replacement_context,
        cross_school_id=other_import_actor_facts.school_id,
        cross_school_batch_id=cross_school_command.batch_id,
    )


def _execute_direct_takeover_bundle(
    scenario: ApplyingImportScenario,
    *,
    missing_component: str | None = None,
    takeover_request_id: UUID | None = None,
    unit_corruption: str | None = None,
    call_final_boundary: bool = True,
    bundle_in_savepoint: bool = False,
) -> tuple[UUID, UUID]:
    """以真实 runtime/UoW 准备接管 bundle，再调用最终固定 CAS 边界。"""

    models = importlib.import_module("app.platform.master_data_imports.models")
    service = scenario.replacement_service
    actor = scenario.replacement_context.actor
    assert type(actor) is StaffActorContext
    identity = service._takeover_idempotency_identity(  # pyright: ignore[reportPrivateUsage]
        scenario.takeover_command,
        scenario.replacement_context,
        actor,
    )
    with service._uow_factory.open(  # pyright: ignore[reportPrivateUsage]
        scenario.replacement_context
    ) as uow:
        now = uow.current_database_time()
        service._authorize(uow, actor, now)  # pyright: ignore[reportPrivateUsage]
        old_record_id = uow.imports.find_active_idempotency_record_id(
            actor.school_id,
            scenario.batch_id,
        )
        assert old_record_id == scenario.old_idempotency_record_id
        old_record = uow.imports.lock_takeover_source(
            actor.school_id,
            scenario.batch_id,
            old_record_id,
        )
        assert old_record is not None
        uow.imports.lock_takeover_assignment(
            actor.school_id,
            scenario.batch_id,
            old_record.record_id,
            old_record.actor_assignment_id,
        )
        new_record = None
        if missing_component != "new_record":
            acquisition = uow.idempotency.acquire_with_origin(identity)
            assert acquisition.created
            new_record = acquisition.record
        batch = uow.imports.lock_batch(actor.school_id, scenario.batch_id)
        assert batch is not None
        units = uow.imports.lock_units(actor.school_id, batch.id)
        assert (
            uow.imports.lock_takeover_by_previous_record(
                actor.school_id,
                old_record.record_id,
            )
            is None
        )
        batch_version_before = batch.version
        batch_version_after = batch.version + 1
        takeover_id = uuid5(
            new_record.record_id if new_record is not None else old_record.record_id,
            "master-data-import-takeover",
        )
        if unit_corruption == "pre_manifest_completed_failed_to_incomplete":
            assert units[0].status == "failed"
            assert units[0].last_attempt_completed is True
            units[0].last_attempt_completed = False
            uow.imports.save_unit(units[0])
        elif unit_corruption == "pre_manifest_subtransaction_completed_failed_to_incomplete":
            assert units[0].status == "failed"
            assert units[0].last_attempt_completed is True
            with uow.session.begin_nested():
                units[0].last_attempt_completed = False
                uow.imports.save_unit(units[0])
        bundle_savepoint = uow.session.begin_nested() if bundle_in_savepoint else None
        try:
            if new_record is not None and missing_component != "takeover":
                uow.imports.add_takeover(
                    models.MasterDataImportTakeover(
                        id=takeover_id,
                        school_id=actor.school_id,
                        batch_id=batch.id,
                        previous_idempotency_record_id=old_record.record_id,
                        new_idempotency_record_id=new_record.record_id,
                        original_actor_user_id=old_record.actor_user_id,
                        original_actor_person_id=old_record.actor_person_id,
                        original_actor_assignment_id=old_record.actor_assignment_id,
                        original_actor_assignment_version=old_record.actor_assignment_version,
                        new_actor_user_id=actor.account_user_id,
                        new_actor_person_id=actor.person_id,
                        new_actor_assignment_id=actor.assignment_id,
                        new_actor_assignment_version=actor.assignment_version,
                        takeover_reason=scenario.takeover_command.takeover_reason,
                        request_id=takeover_request_id or scenario.replacement_context.request_id,
                        batch_version_before=batch_version_before,
                        batch_version_after=batch_version_after,
                    )
                )
            if new_record is not None and missing_component != "units":
                for unit in units:
                    if (
                        unit.last_attempt_idempotency_record_id == old_record.record_id
                        and unit.last_attempt_completed is False
                    ):
                        unit.last_attempt_idempotency_record_id = new_record.record_id
                        uow.imports.save_unit(unit)
                if unit_corruption == "transferred_applied":
                    units[0].status = "applied"
                    uow.imports.save_unit(units[0])
                elif unit_corruption == "transferred_version_changed":
                    units[0].version += 1
                    uow.imports.save_unit(units[0])
                elif unit_corruption == "transferred_completed":
                    units[0].last_attempt_completed = True
                    uow.imports.save_unit(units[0])
                elif unit_corruption == "extra_incomplete":
                    source_unit = units[0]
                    uow.imports.add_unit(
                        models.MasterDataImportUnit(
                            id=uuid4(),
                            school_id=source_unit.school_id,
                            batch_id=source_unit.batch_id,
                            sequence=max(unit.sequence for unit in units) + 1,
                            row_number=max(unit.row_number for unit in units) + 1,
                            unit_kind=source_unit.unit_kind,
                            status="ready",
                            payload=source_unit.payload,
                            payload_sha256=source_unit.payload_sha256,
                            soft_anomalies=[],
                            initial_soft_anomalies=[],
                            decision_reason=None,
                            target_id=None,
                            target_version=None,
                            error_code=None,
                            error_field_code=None,
                            last_attempt_idempotency_record_id=new_record.record_id,
                            last_attempt_completed=False,
                            version=1,
                        )
                    )
            if new_record is not None and missing_component != "batch":
                batch.active_idempotency_record_id = new_record.record_id
                batch.active_action_code = "master_data.import.ordinary_takeover"
                batch.version = batch_version_after
                uow.imports.save_batch(batch)
            if bundle_savepoint is not None:
                bundle_savepoint.commit()
        except BaseException:
            if bundle_savepoint is not None and bundle_savepoint.is_active:
                bundle_savepoint.rollback()
            raise
        if call_final_boundary:
            uow.imports.supersede_takeover_source(
                actor.school_id,
                batch.id,
                old_record.record_id,
                expected_request_sha256=old_record.request_sha256,
                batch_version_before=batch_version_before,
            )
        uow.commit()
    assert new_record is not None
    return takeover_id, new_record.record_id


def _ordinary_same_key_facts(
    roles: PostgresRoles,
    *,
    actor: ImportActorFacts,
    batch_id: UUID,
    external_reference: str,
    request_id: UUID,
) -> tuple[Any, ...]:
    """从 Owner 新连接读取同键并发的唯一业务、审计、Outbox 与命令结果事实。"""

    with roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        row = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM school_people
                 WHERE school_id = %s),
                (SELECT count(*) FROM school_people AS person
                 JOIN master_data_import_units AS scoped_unit
                   ON scoped_unit.school_id = person.school_id
                  AND scoped_unit.target_id = person.id
                 WHERE scoped_unit.school_id = %s
                   AND scoped_unit.batch_id = %s
                   AND scoped_unit.sequence = 1),
                (SELECT count(*) FROM school_person_names
                 WHERE school_id = %s),
                (SELECT count(*) FROM school_person_names AS name
                 JOIN master_data_import_units AS scoped_unit
                   ON scoped_unit.school_id = name.school_id
                  AND scoped_unit.target_id = name.person_id
                 WHERE scoped_unit.school_id = %s
                   AND scoped_unit.batch_id = %s
                   AND scoped_unit.sequence = 1
                   AND name.source_kind = 'person_external_reference'
                   AND name.source_ref = %s),
                (SELECT count(*) FROM audit_logs
                 WHERE school_id = %s AND request_id = %s
                   AND event_type = 'master_data.person.imported'),
                (SELECT count(*) FROM outbox_messages
                 WHERE school_id = %s AND event_type = 'master_data.person.imported'),
                (SELECT count(*) FROM master_data_import_command_outcomes
                 WHERE school_id = %s AND batch_id = %s),
                (SELECT count(*) FROM command_idempotency_records
                 WHERE school_id = %s AND action_code = 'master_data.import.ordinary_confirm'
                   AND resource_id = %s),
                batch.status, batch.succeeded_units, batch.failed_units, batch.pending_units,
                unit.status, unit.last_attempt_completed,
                unit.last_attempt_idempotency_record_id
            FROM master_data_import_batches AS batch
            JOIN master_data_import_units AS unit
              ON unit.school_id = batch.school_id AND unit.batch_id = batch.id
            WHERE batch.school_id = %s AND batch.id = %s AND unit.sequence = 1
            """,
            (
                actor.school_id,
                actor.school_id,
                batch_id,
                actor.school_id,
                actor.school_id,
                batch_id,
                external_reference,
                actor.school_id,
                str(request_id),
                actor.school_id,
                actor.school_id,
                batch_id,
                actor.school_id,
                str(batch_id),
                actor.school_id,
                batch_id,
            ),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _school_person_name_counts(
    roles: PostgresRoles,
    *,
    school_id: UUID,
) -> tuple[int, int]:
    """在普通确认前读取隔离学校的正式 person/name 基线。"""

    with roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        row = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM school_people WHERE school_id = %s),
                (SELECT count(*) FROM school_person_names WHERE school_id = %s)
            """,
            (school_id, school_id),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def _seed_takeover_prestate(
    roles: PostgresRoles,
    *,
    original_actor: ImportActorFacts,
    batch_id: UUID,
    previous_record_id: UUID,
    previous_action: str = "master_data.import.ordinary_resume",
    previous_resource_type: str = "master_data.import_batch",
    previous_resource_id: UUID | None = None,
    unit_id: UUID | None = None,
) -> tuple[object, ...]:
    """以原 actor 事务创建 applying 批次和仍在执行的旧命令。"""

    old_context = _command_context(
        original_actor,
        idempotency_key=f"task8-takeover-old-command-{uuid4().hex}",
    )
    with psycopg.connect(**roles.admin_connection_parameters, autocommit=False) as connection:
        connection.execute("SET LOCAL ROLE campus_ai_runtime")
        set_local_execution_context(cast(Any, connection), _envelope_factory(old_context))
        connection.execute("RESET ROLE")
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO command_idempotency_records (
                id, school_id, action_code, actor_user_id, actor_person_id,
                actor_context_type, actor_context_key, actor_assignment_id,
                actor_assignment_version, resource_type, resource_id,
                idempotency_key_hash, request_sha256, status
            ) VALUES (
                %s, %s, %s, %s, %s,
                'assignment', %s, %s, 1, %s, %s,
                %s, %s, 'in_progress'
            )
            """,
            (
                previous_record_id,
                original_actor.school_id,
                previous_action,
                original_actor.actor_user_id,
                original_actor.actor_person_id,
                f"assignment:{original_actor.actor_assignment_id}:1",
                original_actor.actor_assignment_id,
                previous_resource_type,
                str(previous_resource_id or batch_id),
                sha256(old_context.idempotency_key.encode("utf-8")).hexdigest(),
                "7" * 64,
            ),
        )
        connection.execute(
            """
            INSERT INTO master_data_import_batches (
                id, school_id, mode, source_kind, source_ref, source_sha256,
                filename_summary, source_size_bytes, worksheet_names, status,
                planned_units, succeeded_units, failed_units, pending_units,
                confirmed_by_user_id, confirmed_by_person_id,
                confirmed_by_assignment_id, confirmed_at, request_id,
                idempotency_key_hash, active_idempotency_record_id,
                active_action_code, version
            ) VALUES (
                %s, %s, 'ordinary_atomic_units', 'excel', %s, %s,
                'takeover-audit.xlsx', 1024, '[]'::jsonb, 'applying',
                1, 0, 0, 1, %s, %s, %s, clock_timestamp(), %s,
                %s, %s, 'master_data.import.ordinary_resume', 1
            )
            """,
            (
                batch_id,
                original_actor.school_id,
                f"protected-{uuid4().hex}",
                "9" * 64,
                original_actor.actor_user_id,
                original_actor.actor_person_id,
                original_actor.actor_assignment_id,
                old_context.request_id,
                "a" * 64,
                previous_record_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO master_data_import_units (
                id, school_id, batch_id, sequence, row_number, unit_kind, status,
                payload, payload_sha256, soft_anomalies, initial_soft_anomalies,
                last_attempt_idempotency_record_id, last_attempt_completed, version
            ) VALUES (
                %s, %s, %s, 1, 1, 'person', 'ready',
                '{}'::jsonb, %s, '[]'::jsonb, '[]'::jsonb, %s, false, 1
            )
            """,
            (
                unit_id or uuid4(),
                original_actor.school_id,
                batch_id,
                "b" * 64,
                previous_record_id,
            ),
        )
        confirmer = connection.execute(
            """
            SELECT confirmed_by_user_id, confirmed_by_person_id,
                   confirmed_by_assignment_id, confirmed_at
            FROM master_data_import_batches
            WHERE school_id = %s AND id = %s
            """,
            (original_actor.school_id, batch_id),
        ).fetchone()
        connection.commit()
    assert confirmer is not None
    return tuple(confirmer)


def _insert_takeover_audit_fact_set(
    connection: psycopg.Connection[Any],
    *,
    context: CommandExecutionContext,
    original_actor: ImportActorFacts,
    new_actor: ImportActorFacts,
    batch_id: UUID,
    previous_record_id: UUID,
    new_record_id: UUID,
    takeover_id: UUID,
    violation: str | None = None,
    takeover_reason: str = "仅保存在接管事实中的自由文本",
) -> None:
    """在调用方事务内写入可按字段破坏的接管审计事实集。"""

    connection.execute("SET LOCAL ROLE campus_ai_runtime")
    set_local_execution_context(cast(Any, connection), _envelope_factory(context))
    connection.execute("RESET ROLE")
    connection.execute("SET LOCAL ROLE campus_ai_db_owner")
    previous_result_batch_id = uuid4() if violation == "wrong_previous_result_batch" else batch_id
    new_action = (
        "master_data.import.ordinary_resume"
        if violation == "wrong_new_action"
        else "master_data.import.ordinary_takeover"
    )
    new_resource_type = (
        "school_person" if violation == "wrong_new_resource" else "master_data.import_batch"
    )
    new_resource_id = uuid4() if violation == "wrong_new_resource" else batch_id
    connection.execute(
        """
        INSERT INTO command_idempotency_records (
            id, school_id, action_code, actor_user_id, actor_person_id,
            actor_context_type, actor_context_key, actor_assignment_id,
            actor_assignment_version, resource_type, resource_id,
            idempotency_key_hash, request_sha256, status
        ) VALUES (
            %s, %s, %s, %s, %s, 'assignment', %s, %s, 1, %s, %s,
            %s, %s, 'in_progress'
        )
        """,
        (
            new_record_id,
            new_actor.school_id,
            new_action,
            new_actor.actor_user_id,
            new_actor.actor_person_id,
            f"assignment:{new_actor.actor_assignment_id}:1",
            new_actor.actor_assignment_id,
            new_resource_type,
            str(new_resource_id),
            sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
            "8" * 64,
        ),
    )
    connection.execute(
        """
        UPDATE master_data_import_batches
        SET active_idempotency_record_id = %s,
            active_action_code = 'master_data.import.ordinary_takeover',
            version = 2
        WHERE school_id = %s AND id = %s
        """,
        (new_record_id, original_actor.school_id, batch_id),
    )
    connection.execute(
        """
        UPDATE command_idempotency_records
        SET status = 'terminal_failure',
            result_type = 'master_data.import_outcome',
            result_payload = jsonb_build_object(
                'resource_type', 'master_data.import_batch',
                'resource_id', %s::text,
                'resource_version', 1,
                'outcome_code', 'master_data.import.command_superseded'
            )
        WHERE school_id = %s AND id = %s
        """,
        (
            str(previous_result_batch_id),
            original_actor.school_id,
            previous_record_id,
        ),
    )
    original_user_id = (
        new_actor.actor_user_id
        if violation == "wrong_original_actor"
        else original_actor.actor_user_id
    )
    original_person_id = (
        new_actor.actor_person_id
        if violation == "wrong_original_actor"
        else original_actor.actor_person_id
    )
    original_assignment_id = (
        new_actor.actor_assignment_id
        if violation == "wrong_original_actor"
        else original_actor.actor_assignment_id
    )
    original_version = 2 if violation == "wrong_original_version" else 1
    new_user_id = (
        original_actor.actor_user_id if violation == "wrong_new_actor" else new_actor.actor_user_id
    )
    new_version = 2 if violation == "wrong_new_version" else 1
    takeover_request_id = uuid4() if violation == "wrong_request" else context.request_id
    version_before = 2 if violation == "wrong_batch_versions" else 1
    version_after = 3 if violation == "wrong_batch_versions" else 2
    connection.execute(
        """
        INSERT INTO master_data_import_takeovers (
            id, school_id, batch_id,
            previous_idempotency_record_id, new_idempotency_record_id,
            original_actor_user_id, original_actor_person_id,
            original_actor_assignment_id, original_actor_assignment_version,
            new_actor_user_id, new_actor_person_id,
            new_actor_assignment_id, new_actor_assignment_version,
            takeover_reason, request_id, batch_version_before, batch_version_after
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            takeover_id,
            original_actor.school_id,
            batch_id,
            previous_record_id,
            new_record_id,
            original_user_id,
            original_person_id,
            original_assignment_id,
            original_version,
            new_user_id,
            new_actor.actor_person_id,
            new_actor.actor_assignment_id,
            new_version,
            takeover_reason,
            takeover_request_id,
            version_before,
            version_after,
        ),
    )
    connection.execute(
        """
        UPDATE master_data_import_units
        SET last_attempt_idempotency_record_id = %s
        WHERE school_id = %s
          AND batch_id = %s
          AND last_attempt_idempotency_record_id = %s
          AND last_attempt_completed IS FALSE
        """,
        (
            new_record_id,
            original_actor.school_id,
            batch_id,
            previous_record_id,
        ),
    )


def _append_takeover_audit(
    connection: psycopg.Connection[Any],
    *,
    context: CommandExecutionContext,
    actor: ImportActorFacts,
    batch_id: UUID,
    takeover_id: UUID,
) -> int:
    """在已绑定的调用方事务内直调固定接管审计函数。"""

    row = connection.execute(
        """
        SELECT app.append_master_data_import_audit(
            %s, %s, %s, %s, 1, %s,
            'master_data.import.taken_over', 'master_data_import_takeover',
            %s, %s, NULL,
            jsonb_build_object(
                'state_code', 'ordinary_import.active_command',
                'resource_version', NULL,
                'stable_reference_ids', jsonb_build_array(%s::text),
                'fact_codes', '[]'::jsonb
            ),
            jsonb_build_object(
                'state_code', 'ordinary_import.taken_over',
                'resource_version', NULL,
                'stable_reference_ids', jsonb_build_array(%s::text, %s::text),
                'fact_codes', '["ordinary_import.taken_over"]'::jsonb
            ),
            %s, 'manual.ordinary-import-taken-over'
        )
        """,
        (
            actor.actor_user_id,
            actor.school_id,
            actor.actor_person_id,
            actor.actor_assignment_id,
            sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
            takeover_id,
            batch_id,
            batch_id,
            batch_id,
            takeover_id,
            context.request_id,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _insert_manual_audit_fact_set(
    connection: psycopg.Connection[Any],
    *,
    context: CommandExecutionContext,
    actor: ImportActorFacts,
    person_id: UUID,
    batch_id: UUID,
    unit_id: UUID,
    record_id: UUID,
    actual_same_name_continuation: bool,
    person_version: int = 1,
    record_idempotency_key_hash: str | None = None,
    half_consistent_same_name_state: str | None = None,
) -> None:
    """Insert the minimum reachable manual-import facts in the caller transaction."""

    connection.execute("SET LOCAL ROLE campus_ai_runtime")
    set_local_execution_context(cast(Any, connection), _envelope_factory(context))
    connection.execute("RESET ROLE")
    connection.execute("SET LOCAL ROLE campus_ai_db_owner")
    idempotency_key_hash = (
        record_idempotency_key_hash or sha256(context.idempotency_key.encode("utf-8")).hexdigest()
    )
    connection.execute(
        "UPDATE school_people SET version = %s WHERE school_id = %s AND id = %s",
        (person_version, actor.school_id, person_id),
    )
    connection.execute(
        """
        INSERT INTO command_idempotency_records (
            id, school_id, action_code, actor_user_id, actor_person_id,
            actor_context_type, actor_context_key, actor_assignment_id,
            actor_assignment_version, resource_type, resource_id,
            idempotency_key_hash, request_sha256, status, result_type, result_payload
        ) VALUES (
            %s, %s, 'master_data.import.manual_confirm', %s, %s,
            'assignment', %s, %s, 1, 'master_data.import_batch', %s,
            %s, %s, 'succeeded', 'master_data.import_outcome',
            jsonb_build_object(
                'resource_type', 'master_data.import_batch',
                'resource_id', %s::text,
                'resource_version', 1,
                'outcome_code', 'applied'
            )
        )
        """,
        (
            record_id,
            actor.school_id,
            actor.actor_user_id,
            actor.actor_person_id,
            f"assignment:{actor.actor_assignment_id}:1",
            actor.actor_assignment_id,
            str(batch_id),
            idempotency_key_hash,
            "b" * 64,
            str(batch_id),
        ),
    )
    connection.execute(
        """
        INSERT INTO master_data_import_batches (
            id, school_id, mode, source_kind, source_ref, source_sha256,
            worksheet_names, status, planned_units, succeeded_units,
            failed_units, pending_units, confirmation_card_sha256,
            confirmed_by_user_id, confirmed_by_person_id,
            confirmed_by_assignment_id, confirmed_at, request_id,
            idempotency_key_hash, version
        ) VALUES (
            %s, %s, 'manual_unit', 'manual', %s, %s,
            '[]'::jsonb, 'applied', 1, 1, 0, 0, %s,
            %s, %s, %s, clock_timestamp(), %s, %s, 1
        )
        """,
        (
            batch_id,
            actor.school_id,
            f"manual-audit-{uuid4().hex}",
            "c" * 64,
            "d" * 64,
            actor.actor_user_id,
            actor.actor_person_id,
            actor.actor_assignment_id,
            context.request_id,
            idempotency_key_hash,
        ),
    )
    if half_consistent_same_name_state == "anomaly_without_reason":
        soft_anomalies = '[{"code": "same_display_name"}]'
        decision_reason = None
    elif half_consistent_same_name_state == "reason_without_anomaly":
        soft_anomalies = "[]"
        decision_reason = "arbitrary reason without same-name anomaly"
    else:
        soft_anomalies = (
            '[{"code": "same_display_name"}]' if actual_same_name_continuation else "[]"
        )
        decision_reason = "manual continuation" if actual_same_name_continuation else None
    connection.execute(
        """
        INSERT INTO master_data_import_units (
            id, school_id, batch_id, sequence, row_number, unit_kind,
            status, payload, payload_sha256, soft_anomalies,
            initial_soft_anomalies, decision_reason, target_id, target_version, version
        ) VALUES (
            %s, %s, %s, 1, 1, 'person', 'applied', '{}'::jsonb, %s,
            %s::jsonb, %s::jsonb, %s, %s, %s, 1
        )
        """,
        (
            unit_id,
            actor.school_id,
            batch_id,
            "e" * 64,
            soft_anomalies,
            soft_anomalies,
            decision_reason,
            person_id,
            person_version,
        ),
    )
    connection.execute(
        """
        INSERT INTO master_data_import_command_outcomes (
            id, school_id, idempotency_record_id, batch_id, action_code,
            status, batch_version, succeeded_units, failed_units, executed_units
        ) VALUES (
            %s, %s, %s, %s, 'master_data.import.manual_confirm',
            'applied', 1, ARRAY[1], ARRAY[]::integer[], ARRAY[1]
        )
        """,
        (uuid4(), actor.school_id, record_id, batch_id),
    )


def _append_manual_audit(
    connection: psycopg.Connection[Any],
    *,
    context: CommandExecutionContext,
    actor: ImportActorFacts,
    person_id: UUID,
    batch_id: UUID,
    unit_id: UUID,
    claim_same_name_continuation: bool,
    audit_person_id: UUID | None = None,
    audit_batch_id: UUID | None = None,
    audit_unit_id: UUID | None = None,
    audit_idempotency_key_hash: str | None = None,
    audit_request_id: UUID | None = None,
    reason_code: str = "manual.person-import-confirmed",
) -> int:
    """Call the fixed manual-person audit branch in the caller transaction."""

    fact_codes = (
        '["person.imported", "person.same_name_manually_continued"]'
        if claim_same_name_continuation
        else '["person.imported"]'
    )
    effective_person_id = audit_person_id or person_id
    effective_batch_id = audit_batch_id or batch_id
    effective_unit_id = audit_unit_id or unit_id
    row = connection.execute(
        """
        SELECT app.append_master_data_import_audit(
            %s, %s, %s, %s, 1, %s,
            'master_data.person.imported', 'school_person', %s, %s, %s,
            jsonb_build_object(
                'state_code', 'person.absent', 'resource_version', NULL,
                'stable_reference_ids', '[]'::jsonb, 'fact_codes', '[]'::jsonb
            ),
            jsonb_build_object(
                'state_code', 'person.confirmed', 'resource_version', 1,
                'stable_reference_ids', jsonb_build_array(%s::text),
                'fact_codes', %s::jsonb
            ),
            %s, %s
        )
        """,
        (
            actor.actor_user_id,
            actor.school_id,
            actor.actor_person_id,
            actor.actor_assignment_id,
            audit_idempotency_key_hash
            or sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
            effective_person_id,
            effective_batch_id,
            effective_unit_id,
            effective_person_id,
            fact_codes,
            audit_request_id or context.request_id,
            reason_code,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _stage_manual_import(
    service: Any,
    facts: ImportActorFacts,
    *,
    marker: str,
) -> tuple[Any, CommandExecutionContext]:
    """通过正式 Handler 创建一个待确认的手工人员导入批次。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    preview = service.stage_manual(
        contracts.StageManualPersonUnitCommand(
            source_reference=f"manual-terminal-guard-{marker}",
            unit=contracts.PersonImportUnitDraft(
                row_number=1,
                display_name="终态守卫测试教师",
                external_reference=f"terminal-guard-{marker}",
                valid_from=datetime.now(UTC).replace(microsecond=0),
            ),
        ),
        context=_command_context(
            facts,
            idempotency_key=f"task8-stage-terminal-guard-{marker}",
        ),
    )
    return (
        preview,
        _command_context(
            facts,
            idempotency_key=f"task8-confirm-terminal-guard-{marker}",
        ),
    )


@pytest.fixture(name="terminal_manual_import")
def _terminal_manual_import(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> TerminalImportFacts:
    """通过正式确认 Handler 生成一个 applied 批次。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    service, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    preview, confirmation_context = _stage_manual_import(
        service,
        import_actor_facts,
        marker=marker,
    )
    outcome = service.confirm(
        contracts.ConfirmImportBatchCommand(
            batch_id=preview.batch_id,
            expected_batch_version=preview.batch_version,
            confirmation_card_sha256=preview.confirmation_card_sha256,
            decisions=(),
        ),
        context=confirmation_context,
    )
    assert outcome.status == "applied"
    return TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=confirmation_context,
        school_id=import_actor_facts.school_id,
        batch_id=preview.batch_id,
    )


def _workbook_bytes(*rows: tuple[Any, ...], title: str = "Sheet1") -> bytes:
    """只在测试内生成受控 xlsx 字节，不接触文件系统或真实学校数据。"""

    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = title
    for row in rows:
        worksheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def _protected_source(
    contracts: ModuleType,
    content: bytes,
    *,
    filename: str = "teacher-roster.xlsx",
    worksheets: tuple[str, ...] = ("教师名单",),
) -> ProtectedImportSourceRef:
    """根据受控测试字节生成不可变来源摘要。"""

    return contracts.ProtectedImportSourceRef(
        opaque_ref="protected-test-source-workbook",
        source_sha256=sha256(content).hexdigest(),
        filename_summary=filename,
        size_bytes=len(content),
        worksheet_names=worksheets,
    )


def _with_extra_zip_entry(content: bytes, name: str, payload: bytes) -> bytes:
    """在真实 xlsx ZIP 上增加一个受控条目，用于验证低层拒绝边界。"""

    output = BytesIO()
    with ZipFile(BytesIO(content), "r") as source_archive:
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as target_archive:
            for entry in source_archive.infolist():
                target_archive.writestr(entry, source_archive.read(entry))
            target_archive.writestr(name, payload)
    return output.getvalue()


def _with_replaced_zip_bytes(
    content: bytes,
    *,
    name: str,
    old: bytes,
    new: bytes,
) -> bytes:
    """替换真实 xlsx 中一个条目的受控字节，用于伪造元数据边界。"""

    output = BytesIO()
    found = False
    with ZipFile(BytesIO(content), "r") as source_archive:
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as target_archive:
            for entry in source_archive.infolist():
                payload = source_archive.read(entry)
                if entry.filename == name:
                    assert old in payload
                    payload = payload.replace(old, new, 1)
                    found = True
                target_archive.writestr(entry, payload)
    assert found
    return output.getvalue()


def test_import_modes_and_batch_states_are_closed() -> None:
    """三类导入和状态迁移必须是封闭语义，不能接受任意字符串。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")

    assert get_args(contracts.ImportMode.__value__) == (
        "manual_unit",
        "ordinary_atomic_units",
        "authoritative_set_publish",
    )
    assert get_args(contracts.ImportBatchStatus.__value__) == (
        "source_registered",
        "mapped",
        "preflight_failed",
        "ready_for_confirmation",
        "applying",
        "partially_failed",
        "applied",
        "published",
    )


def test_ordinary_execution_contract_rejects_more_than_five_hundred_units() -> None:
    """一次普通执行组最多 500 个原子单元，不能把 10,000 个事务塞进单次请求。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    units = tuple(
        contracts.PersonImportUnitDraft(
            row_number=index + 1,
            display_name=f"测试教师{index}",
            external_reference=f"T-{index:04d}",
            valid_from=datetime(2026, 8, 1, tzinfo=UTC),
        )
        for index in range(1, 502)
    )
    source = contracts.ProtectedImportSourceRef(
        opaque_ref="protected-five-hundred-boundary",
        source_sha256="a" * 64,
        filename_summary="teacher-roster.xlsx",
        size_bytes=4_096,
        worksheet_names=("教师名单",),
    )
    mapping = contracts.ColumnMappingDraft(
        mapping_version=1,
        entries=(
            contracts.ColumnMappingEntry(
                source_column_index=1,
                source_header="姓名",
                target_field_code="person.display_name",
            ),
            contracts.ColumnMappingEntry(
                source_column_index=2,
                source_header="编号",
                target_field_code="person.external_reference",
            ),
        ),
    )

    with pytest.raises(ValidationError):
        contracts.StageOrdinaryPersonImportCommand(
            source=source,
            worksheet_name="教师名单",
            mapping=mapping,
            units=units,
        )

    with pytest.raises(ValidationError):
        contracts.ConfirmImportBatchCommand(
            batch_id=uuid4(),
            expected_batch_version=1,
            confirmation_card_sha256="b" * 64,
            decisions=tuple(
                contracts.ImportUnitDecision(
                    sequence=index,
                    expected_unit_version=1,
                )
                for index in range(1, 502)
            ),
        )


def test_protected_import_source_is_a_strict_non_authority_reference() -> None:
    """来源契约只保存受保护对象摘要，不能夹带文件字节或客户端学校字段。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    source_type = contracts.ProtectedImportSourceRef
    source = source_type(
        opaque_ref="protected-test-source-001",
        source_sha256="a" * 64,
        filename_summary="teacher-roster.xlsx",
        size_bytes=2_048,
        worksheet_names=("教师名单",),
    )

    assert source.size_bytes == 2_048
    assert "file_bytes" not in type(source).model_fields
    assert "school_id" not in type(source).model_fields

    with pytest.raises(ValidationError):
        source_type(
            opaque_ref="protected-test-source-001",
            source_sha256="a" * 64,
            filename_summary="../teacher-roster.xlsx",
            size_bytes=2_048,
            worksheet_names=("教师名单",),
            school_id=uuid4(),
        )

    with pytest.raises(ValidationError):
        source_type(
            opaque_ref="protected-test-source-001",
            source_sha256="a" * 64,
            filename_summary="teacher-roster.xlsx",
            size_bytes=10 * 1024 * 1024 + 1,
            worksheet_names=("教师名单",),
        )


def test_person_column_mapping_is_explicit_versioned_and_unambiguous() -> None:
    """不同学校表头只能由明确版本映射到首批人员字段，重复目标必须拒绝。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    entry_type = contracts.ColumnMappingEntry
    mapping_type = contracts.ColumnMappingDraft
    mapping = mapping_type(
        mapping_version=1,
        entries=(
            entry_type(
                source_column_index=1,
                source_header="教职工姓名",
                target_field_code="person.display_name",
            ),
            entry_type(
                source_column_index=3,
                source_header="校内编号",
                target_field_code="person.external_reference",
            ),
        ),
    )

    assert mapping.mapping_version == 1
    assert tuple(entry.source_column_index for entry in mapping.entries) == (1, 3)

    with pytest.raises(ValidationError):
        mapping_type(
            mapping_version=1,
            entries=(
                entry_type(
                    source_column_index=1,
                    source_header="姓名",
                    target_field_code="person.display_name",
                ),
                entry_type(
                    source_column_index=2,
                    source_header="别名",
                    target_field_code="person.display_name",
                ),
            ),
        )


def test_ordinary_import_must_select_a_workbook_sheet_from_the_protected_source() -> None:
    """普通导入命令不能引用受保护来源未声明的工作表。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    marker = uuid4().hex
    with pytest.raises(ValidationError, match="工作表"):
        contracts.StageOrdinaryPersonImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"protected-{marker}",
                source_sha256=sha256(marker.encode("utf-8")).hexdigest(),
                filename_summary="synthetic-roster.xlsx",
                size_bytes=1024,
                worksheet_names=("教师名单",),
            ),
            worksheet_name="未声明工作表",
            mapping=contracts.ColumnMappingDraft(
                mapping_version=1,
                entries=(
                    contracts.ColumnMappingEntry(
                        source_column_index=1,
                        source_header="姓名",
                        target_field_code="person.display_name",
                    ),
                    contracts.ColumnMappingEntry(
                        source_column_index=2,
                        source_header="校内编号",
                        target_field_code="person.external_reference",
                    ),
                ),
            ),
            units=(
                contracts.PersonImportUnitDraft(
                    row_number=2,
                    display_name="测试教师",
                    external_reference=f"T-{marker}",
                    valid_from=datetime.now(UTC).replace(microsecond=0),
                ),
            ),
        )


def test_three_stage_commands_cannot_carry_actor_school_or_confirmation_flags() -> None:
    """三类草稿命令只表达候选事实，权威上下文只能由确认阶段服务端注入。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    manual = contracts.StageManualPersonUnitCommand(
        source_reference="manual-card-001",
        unit=contracts.PersonImportUnitDraft(
            row_number=1,
            display_name="测试教师",
            external_reference="T-001",
            valid_from=datetime(2026, 8, 1, tzinfo=UTC),
        ),
    )
    source = contracts.ProtectedImportSourceRef(
        opaque_ref="protected-test-source-002",
        source_sha256="b" * 64,
        filename_summary="calendar.xlsx",
        size_bytes=4_096,
        worksheet_names=("校历",),
    )
    calendar = contracts.StageAuthoritativeCalendarImportCommand(
        source=source,
        draft=contracts.AuthoritativeCalendarDraft(
            display_name="2026秋季校历",
            timezone_name="Asia/Shanghai",
            valid_from=datetime(2026, 8, 31, tzinfo=UTC),
            valid_to=None,
            days=(
                contracts.CalendarDayDraft(
                    local_date=date(2026, 9, 1),
                    day_kind="working",
                    actual_start_time=time(8, 0),
                    actual_end_time=time(17, 30),
                    note=None,
                ),
            ),
        ),
    )

    assert manual.mode == "manual_unit"
    assert calendar.mode == "authoritative_set_publish"
    for command_type in (
        contracts.StageManualPersonUnitCommand,
        contracts.StageOrdinaryPersonImportCommand,
        contracts.StageAuthoritativeCalendarImportCommand,
    ):
        assert {
            "school_id",
            "actor",
            "assignment_id",
            "idempotency_key",
            "confirmed",
        }.isdisjoint(command_type.model_fields)

    with pytest.raises(ValidationError):
        contracts.StageManualPersonUnitCommand(
            source_reference="manual-card-001",
            unit=manual.unit,
            school_id=uuid4(),
            confirmed=True,
        )


def test_takeover_command_is_strict_and_carries_no_authority_facts() -> None:
    """接管命令只绑定预期版本和人工理由，不接受权威上下文。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    forbidden = {
        "school_id",
        "actor_user_id",
        "actor_person_id",
        "actor_assignment_id",
        "permission",
        "semantic_role_code",
        "previous_idempotency_record_id",
        "original_assignment_invalid",
    }
    assert forbidden.isdisjoint(contracts.TakeOverOrdinaryImportCommand.model_fields)
    valid_payload = {
        "batch_id": uuid4(),
        "expected_batch_version": 1,
        "confirmation_card_sha256": "a" * 64,
        "units": (
            {
                "sequence": 1,
                "expected_unit_version": 1,
                "continue_reason": None,
            },
        ),
        "takeover_reason": "原活动任职已失效，由现任管理员继续",
    }
    with pytest.raises(ValidationError) as whitespace_error:
        contracts.TakeOverOrdinaryImportCommand.model_validate(
            {**valid_payload, "takeover_reason": "   "}
        )
    assert [error["loc"] for error in whitespace_error.value.errors()] == [("takeover_reason",)]

    with pytest.raises(ValidationError) as extra_field_error:
        contracts.TakeOverOrdinaryImportCommand.model_validate(
            {**valid_payload, "school_id": str(uuid4())}
        )
    assert [error["loc"] for error in extra_field_error.value.errors()] == [("school_id",)]


def test_takeover_command_requires_unique_bounded_unit_versions() -> None:
    """接管的未完成单元必须逐项绑定版本，且同一序号只能选择一次。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    unit = {
        "sequence": 1,
        "expected_unit_version": 2,
        "continue_reason": "已核对同名提示并继续",
    }
    with pytest.raises(ValidationError, match="只能选择一次"):
        contracts.TakeOverOrdinaryImportCommand(
            batch_id=uuid4(),
            expected_batch_version=2,
            confirmation_card_sha256="b" * 64,
            units=(unit, unit),
            takeover_reason="原活动任职已经失效，由现任管理员继续未完成单元",
        )


def test_takeover_idempotency_owner_projection_excludes_arbitrary_payloads() -> None:
    """接管只读取稳定命令归属字段，不暴露原始 JSON 载荷或自由文本。"""

    contracts = importlib.import_module("app.platform.idempotency.contracts")
    projection_type = contracts.IdempotencyRecordOwnerProjection
    assert {
        "result_payload",
        "takeover_reason",
        "request_payload",
    }.isdisjoint(projection_type.model_fields)
    assert {
        "action_code",
        "actor_user_id",
        "actor_person_id",
        "actor_context_type",
        "actor_assignment_id",
        "actor_assignment_version",
        "resource_type",
        "resource_id",
        "idempotency_key_hash",
    }.issubset(projection_type.model_fields)


def test_different_excel_headers_require_explicit_mapping() -> None:
    """Inspector 只能描述结构，不得猜字段映射或产生正式写入。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    content = _workbook_bytes(
        ("教职工姓名", "班别", "校内编号"),
        ("测试教师", "初一3班", "T-001"),
        title="教师名单",
    )
    source = _protected_source(contracts, content)

    inspection = excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)

    assert inspection.status == "source_registered"
    assert inspection.proposed_mapping is None
    assert inspection.formal_writes == 0
    assert inspection.worksheets[0].headers == (
        "教职工姓名",
        "班别",
        "校内编号",
    )
    assert inspection.worksheets[0].sample_value_types == (
        ("text",),
        ("text",),
        ("text",),
    )
    assert "测试教师" not in inspection.model_dump_json()


def test_explicit_mapping_generates_typed_person_draft_and_rejects_blank_required_cell() -> None:
    """只有服务端按人工映射重读同一来源后，才能生成与真实行号绑定的人员草稿。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    mapping = contracts.ColumnMappingDraft(
        mapping_version=1,
        entries=(
            contracts.ColumnMappingEntry(
                source_column_index=1,
                source_header="教职工姓名",
                target_field_code="person.display_name",
            ),
            contracts.ColumnMappingEntry(
                source_column_index=3,
                source_header="校内编号",
                target_field_code="person.external_reference",
            ),
        ),
    )
    content = _workbook_bytes(
        ("教职工姓名", "班别", "校内编号"),
        ("测试教师", "初一3班", "T-001"),
        title="教师名单",
    )
    source = _protected_source(contracts, content)
    mapped = excel.RestrictedExcelPersonMapper().map(
        BytesIO(content),
        source=source,
        worksheet_name="教师名单",
        mapping=mapping,
        default_valid_from=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert mapped.units[0].row_number == 2
    assert mapped.units[0].display_name == "测试教师"
    assert mapped.units[0].external_reference == "T-001"

    invalid = _workbook_bytes(
        ("教职工姓名", "班别", "校内编号"),
        ("测试教师", "初一3班", None),
        title="教师名单",
    )
    invalid_source = _protected_source(contracts, invalid)
    with pytest.raises(excel.ImportWorkbookRejected, match="第 2 行.*校内编号") as error:
        excel.RestrictedExcelPersonMapper().map(
            BytesIO(invalid),
            source=invalid_source,
            worksheet_name="教师名单",
            mapping=mapping,
            default_valid_from=datetime(2026, 8, 1, tzinfo=UTC),
        )
    assert "测试教师" not in str(error.value)


def test_excel_mapper_accepts_explicit_offset_valid_from_text() -> None:
    """Excel 无法可靠保存时区对象，公开生效时间映射使用含偏移量的 ISO 文本。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    content = _workbook_bytes(
        ("教职工姓名", "校内编号", "生效时间"),
        ("测试教师", "T-001", "2026-08-01T08:30:00+08:00"),
        title="教师名单",
    )
    source = _protected_source(contracts, content)
    mapped = excel.RestrictedExcelPersonMapper().map(
        BytesIO(content),
        source=source,
        worksheet_name="教师名单",
        mapping=contracts.ColumnMappingDraft(
            mapping_version=1,
            entries=(
                contracts.ColumnMappingEntry(
                    source_column_index=1,
                    source_header="教职工姓名",
                    target_field_code="person.display_name",
                ),
                contracts.ColumnMappingEntry(
                    source_column_index=2,
                    source_header="校内编号",
                    target_field_code="person.external_reference",
                ),
                contracts.ColumnMappingEntry(
                    source_column_index=3,
                    source_header="生效时间",
                    target_field_code="person.valid_from",
                ),
            ),
        ),
        default_valid_from=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert mapped.units[0].valid_from.isoformat() == "2026-08-01T08:30:00+08:00"


@pytest.mark.parametrize(
    ("display_name", "external_reference", "safe_field_name"),
    [
        ("SENSITIVE-NAME-" + "甲" * 300, "T-001", "教职工姓名"),
        ("测试教师", "SENSITIVE-REFERENCE-" + "9" * 300, "校内编号"),
    ],
)
def test_excel_mapper_redacts_values_rejected_by_contracts(
    display_name: str,
    external_reference: str,
    safe_field_name: str,
) -> None:
    """契约长度错误只能暴露行号和中文字段名，不能把人员原文带入异常链。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    mapping = contracts.ColumnMappingDraft(
        mapping_version=1,
        entries=(
            contracts.ColumnMappingEntry(
                source_column_index=1,
                source_header="教职工姓名",
                target_field_code="person.display_name",
            ),
            contracts.ColumnMappingEntry(
                source_column_index=2,
                source_header="校内编号",
                target_field_code="person.external_reference",
            ),
        ),
    )
    content = _workbook_bytes(
        ("教职工姓名", "校内编号"),
        (display_name, external_reference),
        title="教师名单",
    )
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match=f"第 2 行.*{safe_field_name}") as error:
        excel.RestrictedExcelPersonMapper().map(
            BytesIO(content),
            source=source,
            worksheet_name="教师名单",
            mapping=mapping,
            default_valid_from=datetime(2026, 8, 1, tzinfo=UTC),
        )

    assert "SENSITIVE-" not in str(error.value)
    assert error.value.__cause__ is None


def test_excel_inspector_rejects_formula_cells() -> None:
    """公式结果可能隐藏外部或动态内容，首批 Inspector 必须失败关闭。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    content = _workbook_bytes(
        ("教职工姓名", "校内编号"),
        ("测试教师", "=1+1"),
        title="教师名单",
    )
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="公式"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


def test_excel_inspector_rejects_formula_after_type_sample_window() -> None:
    """类型抽样结束后仍须扫描并拒绝公式，不能把抽样窗口当安全边界。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    safe_rows = tuple((f"测试教师{index}", f"T-{index:03d}") for index in range(1, 21))
    content = _workbook_bytes(
        ("教职工姓名", "校内编号"),
        *safe_rows,
        ("后段测试教师", "=1+1"),
        title="教师名单",
    )
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="公式"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


def test_excel_inspector_rejects_formula_header() -> None:
    """表头也是工作簿内容，不能用公式伪装成普通字段名。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    content = _workbook_bytes(
        ("=1+1", "校内编号"),
        ("测试教师", "T-001"),
        title="教师名单",
    )
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="公式"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


def test_excel_inspector_rejects_formula_hidden_after_forged_dimension() -> None:
    """worksheet dimension 可被伪造，压缩包层仍必须发现范围外公式。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    base = _workbook_bytes(
        ("教职工姓名", "校内编号"),
        ("测试教师", "T-001"),
        ("隐藏教师", "=1+1"),
        title="教师名单",
    )
    content = _with_replaced_zip_bytes(
        base,
        name="xl/worksheets/sheet1.xml",
        old=b'<dimension ref="A1:B3"',
        new=b'<dimension ref="A1:B2"',
    )
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="公式"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


@pytest.mark.parametrize(
    ("rows", "dimension", "max_rows", "max_columns", "message"),
    (
        (
            (("教职工姓名", "校内编号"), ("甲", "T-001"), ("乙", "T-002")),
            (b'A1:B3"', b'A1:B2"'),
            2,
            100,
            "行数",
        ),
        (
            (("教职工姓名", "校内编号", "备注"), ("甲", "T-001", "在职")),
            (b'A1:C2"', b'A1:B2"'),
            10_000,
            2,
            "列数",
        ),
    ),
)
def test_excel_inspector_rejects_text_outside_forged_dimension(
    rows: tuple[tuple[str, ...], ...],
    dimension: tuple[bytes, bytes],
    max_rows: int,
    max_columns: int,
    message: str,
) -> None:
    """纯文本也必须按 XML 实际坐标执行行列预算，不能依赖可伪造 dimension。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    base = _workbook_bytes(*rows, title="教师名单")
    content = _with_replaced_zip_bytes(
        base,
        name="xl/worksheets/sheet1.xml",
        old=dimension[0],
        new=dimension[1],
    )
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match=message):
        excel.RestrictedExcelInspector(
            max_rows=max_rows,
            max_columns=max_columns,
        ).inspect(BytesIO(content), source=source)


def test_excel_inspector_rejects_unbounded_row_coordinate_without_leaking_value_error() -> None:
    """恶意超长坐标必须受控拒绝，不能把 Python 整数解析异常变成 500。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    base = _workbook_bytes(
        ("教职工姓名", "校内编号"),
        ("测试教师", "T-001"),
        title="教师名单",
    )
    content = _with_replaced_zip_bytes(
        base,
        name="xl/worksheets/sheet1.xml",
        old=b'<row r="2"',
        new=b'<row r="' + b"9" * 5_001 + b'"',
    )
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="行数") as error:
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)

    assert error.value.__cause__ is None


def test_excel_inspector_rejects_duplicate_row_elements_over_budget() -> None:
    """重复伪造同一行不能绕过实际 XML 行元素预算。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    base = _workbook_bytes(
        ("教职工姓名", "校内编号"),
        ("测试教师", "T-001"),
        title="教师名单",
    )
    duplicate_row = b'<row r="2"><c r="A2" t="inlineStr"><is><t>x</t></is></c></row>'
    content = _with_replaced_zip_bytes(
        base,
        name="xl/worksheets/sheet1.xml",
        old=b"</sheetData>",
        new=duplicate_row * 10_001 + b"</sheetData>",
    )
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="重复|顺序|行数"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


def test_excel_inspector_rejects_macro_enabled_filename() -> None:
    """即使内容可解析，首批也不能接受可能携带宏的 xlsm 来源。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    content = _workbook_bytes(
        ("教职工姓名", "校内编号"),
        ("测试教师", "T-001"),
        title="教师名单",
    )
    source = _protected_source(contracts, content, filename="teacher-roster.xlsm")

    with pytest.raises(excel.ImportWorkbookRejected, match="xlsx"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


def test_excel_inspector_rejects_merged_cells_as_ambiguous() -> None:
    """合并单元格不能依赖视觉布局猜测列归属。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = "教师名单"
    worksheet.append(("教师信息", None))
    worksheet.merge_cells("A1:B1")
    worksheet.append(("测试教师", "T-001"))
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    content = stream.getvalue()
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="合并"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


def test_excel_inspector_rejects_namespaced_merge_cells() -> None:
    """同一 SpreadsheetML 元素换成命名空间前缀后仍必须按 local-name 拒绝。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = "教师名单"
    worksheet.append(("教师信息", None))
    worksheet.merge_cells("A1:B1")
    worksheet.append(("测试教师", "T-001"))
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    namespace = b"http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    content = _with_replaced_zip_bytes(
        stream.getvalue(),
        name="xl/worksheets/sheet1.xml",
        old=b'<worksheet xmlns="' + namespace + b'"',
        new=b'<worksheet xmlns="' + namespace + b'" xmlns:x="' + namespace + b'"',
    )
    content = _with_replaced_zip_bytes(
        content,
        name="xl/worksheets/sheet1.xml",
        old=b"<mergeCells",
        new=b"<x:mergeCells",
    )
    content = _with_replaced_zip_bytes(
        content,
        name="xl/worksheets/sheet1.xml",
        old=b"</mergeCells>",
        new=b"</x:mergeCells>",
    )
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="合并"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


def test_excel_inspector_rejects_external_link_parts() -> None:
    """即使 openpyxl 忽略外链，Inspector 也必须在解包层失败关闭。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    base = _workbook_bytes(
        ("教职工姓名", "校内编号"),
        ("测试教师", "T-001"),
        title="教师名单",
    )
    content = _with_extra_zip_entry(
        base,
        "xl/externalLinks/externalLink1.xml",
        b"<?xml version='1.0'?><externalLink/>",
    )
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="外部链接"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


def test_excel_inspector_rejects_standard_external_hyperlink_relationship() -> None:
    """标准 worksheet 超链接通过 .rels 指向外部网络时也必须失败关闭。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = "教师名单"
    worksheet.append(("教职工姓名", "校内编号"))
    worksheet.append(("测试教师", "T-001"))
    worksheet["A2"].hyperlink = "https://example.com/private"
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    content = stream.getvalue()
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="外部链接"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


def test_excel_inspector_rejects_embedded_macro_hidden_in_xlsx() -> None:
    """只检查扩展名不够，伪装成 xlsx 的宏条目也必须拒绝。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    base = _workbook_bytes(
        ("教职工姓名", "校内编号"),
        ("测试教师", "T-001"),
        title="教师名单",
    )
    content = _with_extra_zip_entry(base, "xl/vbaProject.bin", b"test-only-macro-marker")
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="宏"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


def test_excel_inspector_maps_invalid_zip_to_safe_rejection() -> None:
    """损坏来源只能返回固定安全错误，不能泄露 zip/openpyxl 内部异常。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    content = b"not-an-xlsx-archive"
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="有效的 xlsx"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


def test_excel_inspector_rejects_archive_expansion_over_budget() -> None:
    """压缩后很小的来源也不能让解压内容无限膨胀。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    base = _workbook_bytes(
        ("教职工姓名", "校内编号"),
        ("测试教师", "T-001"),
        title="教师名单",
    )
    content = _with_extra_zip_entry(base, "xl/media/test.bin", b"0" * 2_048)
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="解压"):
        excel.RestrictedExcelInspector(max_uncompressed_bytes=1_024).inspect(
            BytesIO(content),
            source=source,
        )


@pytest.mark.parametrize(
    ("rows", "max_rows", "max_columns", "message"),
    [
        (
            (
                ("教职工姓名", "校内编号"),
                ("测试教师甲", "T-001"),
                ("测试教师乙", "T-002"),
            ),
            2,
            100,
            "行数",
        ),
        (
            (
                ("教职工姓名", "校内编号", "备注"),
                ("测试教师", "T-001", "测试"),
            ),
            10_000,
            2,
            "列数",
        ),
    ],
)
def test_excel_inspector_enforces_configured_row_and_column_budgets(
    rows: tuple[tuple[object, ...], ...],
    max_rows: int,
    max_columns: int,
    message: str,
) -> None:
    """行列上限必须在遍历工作表前失败关闭。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    content = _workbook_bytes(*rows, title="教师名单")
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match=message):
        excel.RestrictedExcelInspector(
            max_rows=max_rows,
            max_columns=max_columns,
        ).inspect(BytesIO(content), source=source)


def test_runtime_settings_fix_import_file_budget_at_ten_mib() -> None:
    """部署配置不能悄悄放大首批 Excel 文件上限。"""

    common = {
        "app_env": "test",
        "postgres_host": "127.0.0.1",
        "postgres_port": 5432,
        "postgres_db": "campus_ai_import_test",
        "postgres_user": "campus_ai_runtime",
        "postgres_password": "test-only-password",
        "rls_context_signing_key": "test-only-context-signing-key-32-bytes",
        "redis_port": 6379,
        "redis_password": "test-only-redis-password",
    }

    settings = Settings(**common, master_data_import_max_bytes=10 * 1024 * 1024)
    assert settings.master_data_import_max_bytes == 10 * 1024 * 1024

    with pytest.raises(ValidationError):
        Settings(
            **common,
            master_data_import_max_bytes=10 * 1024 * 1024 + 1,  # pyright: ignore[reportArgumentType]
        )


def test_project_metadata_declares_import_package_and_bounded_openpyxl() -> None:
    """正式安装必须包含导入包，并把 Excel 解析器限制在已批准主版本。"""

    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text("utf-8"))

    assert "openpyxl>=3.1,<4" in project["project"]["dependencies"]
    assert "app.platform.master_data_imports" in project["tool"]["setuptools"]["packages"]


def test_excel_inspector_rejects_stream_over_configured_byte_budget() -> None:
    """Inspector 必须在完整解析前拒绝超过配置字节预算的来源。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    content = _workbook_bytes(
        ("教职工姓名", "校内编号"),
        ("测试教师", "T-001"),
        title="教师名单",
    )
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="超过"):
        excel.RestrictedExcelInspector(max_bytes=len(content) - 1).inspect(
            BytesIO(content),
            source=source,
        )


@pytest.mark.parametrize("changed_field", ["source_sha256", "size_bytes"])
def test_excel_inspector_binds_content_to_protected_source_summary(changed_field: str) -> None:
    """来源摘要任一字段不一致都不能继续解析。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    content = _workbook_bytes(
        ("教职工姓名", "校内编号"),
        ("测试教师", "T-001"),
        title="教师名单",
    )
    source = _protected_source(contracts, content)
    replacement: object = "c" * 64 if changed_field == "source_sha256" else len(content) + 1
    changed_source = source.model_copy(update={changed_field: replacement})

    with pytest.raises(excel.ImportWorkbookRejected, match="摘要"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=changed_source)


def test_excel_inspector_binds_actual_worksheet_set_to_source() -> None:
    """工作表名称或顺序变化意味着来源已变，不能沿用旧映射。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    content = _workbook_bytes(
        ("教职工姓名", "校内编号"),
        ("测试教师", "T-001"),
        title="教师名单",
    )
    source = _protected_source(contracts, content, worksheets=("旧工作表名",))

    with pytest.raises(excel.ImportWorkbookRejected, match="工作表集合"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


def test_excel_inspector_rejects_empty_required_header() -> None:
    """空表头不能让映射界面猜测列含义。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    content = _workbook_bytes(
        ("教职工姓名", None),
        ("测试教师", "T-001"),
        title="教师名单",
    )
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected, match="非空文本表头"):
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)


def test_excel_inspector_redacts_overlong_sensitive_header() -> None:
    """输出契约拒绝超长表头时，异常不能携带原始敏感表头。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    sensitive_header = "SENSITIVE-HEADER-" + "隐私" * 120
    content = _workbook_bytes(
        (sensitive_header, "校内编号"),
        ("测试教师", "T-001"),
        title="教师名单",
    )
    source = _protected_source(contracts, content)

    with pytest.raises(excel.ImportWorkbookRejected) as captured:
        excel.RestrictedExcelInspector().inspect(BytesIO(content), source=source)
    assert "SENSITIVE-HEADER" not in str(captured.value)
    assert "隐私" not in str(captured.value)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("assignment", "value_sql"),
    [
        ("status", "'partially_failed'"),
        ("succeeded_units", "0"),
        ("confirmed_by_user_id", "confirmed_by_user_id + 1"),
        ("confirmed_by_person_id", "gen_random_uuid()"),
        ("confirmed_by_assignment_id", "gen_random_uuid()"),
        ("confirmed_at", "clock_timestamp() + interval '1 hour'"),
        ("active_idempotency_record_id", "gen_random_uuid()"),
        ("version", "version + 1"),
    ],
)
def _retired_legacy_test_runtime_cannot_mutate_any_terminal_import_batch_fact(
    terminal_manual_import: TerminalImportFacts,
    assignment: str,
    value_sql: str,
) -> None:
    """运行账号不能通过任何受保护列把终态批次改写或往返降级。"""

    before = terminal_manual_import.snapshot()
    with terminal_manual_import.uow_factory.open(terminal_manual_import.context) as uow:
        with pytest.raises(DBAPIError) as captured:
            uow.session.execute(
                text(
                    f"UPDATE master_data_import_batches SET {assignment} = {value_sql} "
                    "WHERE school_id = :school_id AND id = :batch_id"
                ),
                {
                    "school_id": terminal_manual_import.school_id,
                    "batch_id": terminal_manual_import.batch_id,
                },
            )
            uow.commit()
    assert getattr(captured.value.orig, "sqlstate", None) == "55000"
    assert terminal_manual_import.snapshot() == before


@pytest.mark.integration
def _retired_legacy_test_runtime_confirmation_must_match_trusted_actor_assignment(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """首次确认归属必须等于可信 GUC 中的用户、人员和有效任职。"""

    service, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    preview, confirmation_context = _stage_manual_import(
        service,
        import_actor_facts,
        marker=marker,
    )
    other_actor = _seed_same_school_import_actor(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
    )
    facts = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=confirmation_context,
        school_id=import_actor_facts.school_id,
        batch_id=preview.batch_id,
    )
    before = facts.snapshot()

    with uow_factory.open(confirmation_context) as uow:
        with pytest.raises(DBAPIError) as captured:
            uow.session.execute(
                text(
                    """
                    UPDATE master_data_import_batches
                    SET status = 'applying',
                        confirmed_by_user_id = :other_user_id,
                        confirmed_by_person_id = :other_person_id,
                        confirmed_by_assignment_id = :other_assignment_id,
                        confirmed_at = clock_timestamp(),
                        version = version + 1
                    WHERE school_id = :school_id AND id = :batch_id
                    """
                ),
                {
                    "other_user_id": other_actor.actor_user_id,
                    "other_person_id": other_actor.actor_person_id,
                    "other_assignment_id": other_actor.actor_assignment_id,
                    "school_id": import_actor_facts.school_id,
                    "batch_id": preview.batch_id,
                },
            )
            uow.commit()
    assert getattr(captured.value.orig, "sqlstate", None) == "28000"
    assert facts.snapshot() == before


@pytest.mark.integration
def _retired_legacy_test_runtime_insert_confirmation_must_match_trusted_actor_assignment(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """运行账号 INSERT 已确认态批次时也必须绑定当前可信任职。"""

    service, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    preview, confirmation_context = _stage_manual_import(
        service,
        import_actor_facts,
        marker=marker,
    )
    other_actor = _seed_same_school_import_actor(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
    )
    inserted_batch_id = uuid4()
    facts = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=confirmation_context,
        school_id=import_actor_facts.school_id,
        batch_id=inserted_batch_id,
    )
    before = facts.snapshot()

    with uow_factory.open(confirmation_context) as uow:
        with pytest.raises(DBAPIError) as captured:
            uow.session.execute(
                text(
                    """
                    INSERT INTO master_data_import_batches (
                        id, school_id, mode, source_kind, source_ref, source_sha256,
                        filename_summary, source_size_bytes, worksheet_names,
                        mapping_version, mapping_payload, status, planned_units,
                        succeeded_units, failed_units, pending_units,
                        confirmation_card_sha256, confirmed_by_user_id,
                        confirmed_by_person_id, confirmed_by_assignment_id, confirmed_at,
                        request_id, idempotency_key_hash, active_idempotency_record_id,
                        active_action_code, version
                    )
                    SELECT
                        :inserted_batch_id, source.school_id, source.mode,
                        source.source_kind, :source_ref, source.source_sha256,
                        source.filename_summary, source.source_size_bytes,
                        source.worksheet_names, source.mapping_version,
                        source.mapping_payload, 'applying', source.planned_units,
                        0, 0, source.planned_units, source.confirmation_card_sha256,
                        :other_user_id, :other_person_id, :other_assignment_id,
                        clock_timestamp(), :request_id, :idempotency_key_hash,
                        NULL, NULL, 1
                    FROM master_data_import_batches AS source
                    WHERE source.school_id = :school_id AND source.id = :source_batch_id
                    """
                ),
                {
                    "inserted_batch_id": inserted_batch_id,
                    "source_ref": f"runtime-confirmed-insert-{marker}",
                    "other_user_id": other_actor.actor_user_id,
                    "other_person_id": other_actor.actor_person_id,
                    "other_assignment_id": other_actor.actor_assignment_id,
                    "request_id": uuid4(),
                    "idempotency_key_hash": sha256(
                        f"runtime-confirmed-insert-{marker}".encode()
                    ).hexdigest(),
                    "school_id": import_actor_facts.school_id,
                    "source_batch_id": preview.batch_id,
                },
            )
            uow.commit()
    assert getattr(captured.value.orig, "sqlstate", None) == "28000"
    assert facts.snapshot() == before


@pytest.mark.integration
@pytest.mark.parametrize(
    "inconsistency",
    (
        "terminal_batch_with_pending_unit",
        "terminal_batch_with_wrong_counts",
        "outcome_executed_not_union",
        "idempotency_result_mismatch",
    ),
)
def _retired_legacy_test_terminal_import_batch_requires_matching_units_outcome_and_idempotency_result(  # noqa: E501
    inconsistency: str,
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """终态批次只允许与单元、不可变结果及幂等安全结果共同一致地提交。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    models = importlib.import_module("app.platform.master_data_imports.models")
    repository = importlib.import_module("app.platform.master_data_imports.repository")
    idempotency_repository = importlib.import_module("app.platform.idempotency.repository")
    service, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    preview, confirmation_context = _stage_manual_import(
        service,
        import_actor_facts,
        marker=marker,
    )
    facts = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=confirmation_context,
        school_id=import_actor_facts.school_id,
        batch_id=preview.batch_id,
    )
    before = facts.snapshot()

    if inconsistency == "terminal_batch_with_pending_unit":
        original_save_unit = repository.SqlAlchemyMasterDataImportRepository.save_unit

        def save_pending_unit(self: Any, unit: Any) -> None:
            unit.status = "pending"
            unit.target_id = None
            unit.target_version = None
            original_save_unit(self, unit)

        monkeypatch.setattr(
            repository.SqlAlchemyMasterDataImportRepository,
            "save_unit",
            save_pending_unit,
        )
    elif inconsistency == "terminal_batch_with_wrong_counts":
        original_save_unit = repository.SqlAlchemyMasterDataImportRepository.save_unit

        def save_unit_with_wrong_batch_counts(self: Any, unit: Any) -> None:
            batch = next(
                item
                for item in self._session.identity_map.values()
                if isinstance(item, models.MasterDataImportBatch) and item.id == unit.batch_id
            )
            batch.succeeded_units = 0
            batch.failed_units = 1
            original_save_unit(self, unit)

        monkeypatch.setattr(
            repository.SqlAlchemyMasterDataImportRepository,
            "save_unit",
            save_unit_with_wrong_batch_counts,
        )
    elif inconsistency == "outcome_executed_not_union":
        original_add_outcome = repository.SqlAlchemyMasterDataImportRepository.add_outcome

        def add_invalid_outcome(self: Any, outcome: Any) -> None:
            outcome.executed_units = []
            original_add_outcome(self, outcome)

        monkeypatch.setattr(
            repository.SqlAlchemyMasterDataImportRepository,
            "add_outcome",
            add_invalid_outcome,
        )
    else:
        original_mark_succeeded = (
            idempotency_repository.SqlAlchemyCommandIdempotencyRepository.mark_succeeded
        )

        def mark_succeeded_with_mismatch(
            self: Any,
            identity: Any,
            *,
            result_type: Any,
            result: Any,
        ) -> Any:
            return original_mark_succeeded(
                self,
                identity,
                result_type=result_type,
                result=result.model_copy(update={"resource_version": result.resource_version + 1}),
            )

        monkeypatch.setattr(
            idempotency_repository.SqlAlchemyCommandIdempotencyRepository,
            "mark_succeeded",
            mark_succeeded_with_mismatch,
        )

    with pytest.raises(DBAPIError) as captured:
        service.confirm(
            contracts.ConfirmImportBatchCommand(
                batch_id=preview.batch_id,
                expected_batch_version=preview.batch_version,
                confirmation_card_sha256=preview.confirmation_card_sha256,
                decisions=(),
            ),
            context=confirmation_context,
        )
    assert getattr(captured.value.orig, "sqlstate", None) == "23514"
    assert facts.snapshot() == before


@pytest.mark.integration
def _retired_legacy_test_import_tables_and_takeover_table_have_tenant_rls_composite_fk_and_minimum_runtime_acl(  # noqa: E501
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    """导入草稿表必须从创建起就具备租户隔离和最小运行账号权限。"""

    connection = current_owner_connection
    table_rows = connection.execute(
        """
        SELECT relation.relname,
               owner.rolname,
               relation.relrowsecurity,
               relation.relforcerowsecurity
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_roles AS owner ON owner.oid = relation.relowner
        WHERE namespace.nspname = 'public'
          AND relation.relname IN (
              'master_data_import_batches',
              'master_data_import_units',
              'master_data_import_command_outcomes',
              'master_data_import_takeovers'
          )
        ORDER BY relation.relname
        """
    ).fetchall()
    assert table_rows == [
        ("master_data_import_batches", "campus_ai_db_owner", True, True),
        ("master_data_import_command_outcomes", "campus_ai_db_owner", True, True),
        ("master_data_import_takeovers", "campus_ai_db_owner", True, True),
        ("master_data_import_units", "campus_ai_db_owner", True, True),
    ]

    constraints = {
        row[0]
        for row in connection.execute(
            """
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_schema = 'public'
              AND table_name IN (
                  'master_data_import_batches',
                  'master_data_import_units',
                  'master_data_import_command_outcomes',
                  'master_data_import_takeovers'
              )
            """
        ).fetchall()
    }
    assert {
        "fk_master_data_import_batches_school",
        "fk_master_data_import_batches_confirmer_person",
        "fk_master_data_import_batches_confirmer_assignment",
        "fk_master_data_import_batches_active_idempotency",
        "fk_master_data_import_units_batch",
        "fk_master_data_import_units_last_attempt_idempotency",
        "fk_master_data_import_command_outcomes_batch",
        "fk_master_data_import_command_outcomes_idempotency",
        "uq_master_data_import_batches_school_id_id",
        "uq_master_data_import_units_school_id_id",
        "uq_master_data_import_units_batch_sequence",
        "uq_master_data_import_command_outcomes_school_id_id",
        "uq_master_data_import_command_outcomes_idempotency",
        "fk_master_data_import_takeovers_batch",
        "fk_master_data_import_takeovers_previous_idempotency",
        "fk_master_data_import_takeovers_new_idempotency",
        "fk_master_data_import_takeovers_original_person",
        "fk_master_data_import_takeovers_original_assignment",
        "fk_master_data_import_takeovers_new_person",
        "fk_master_data_import_takeovers_new_assignment",
        "uq_master_data_import_takeovers_previous_command",
        "uq_master_data_import_takeovers_new_command",
        "ck_master_data_import_takeovers_distinct_commands",
        "ck_master_data_import_batches_mode",
        "ck_master_data_import_batches_status",
        "ck_master_data_import_batches_counts",
        "ck_master_data_import_batches_confirmation_state",
        "ck_master_data_import_units_status",
        "ck_master_data_import_units_sequence_positive",
    }.issubset(constraints)

    policies = {
        (row[0], row[1])
        for row in connection.execute(
            """
            SELECT tablename, policyname
            FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename IN (
                  'master_data_import_batches',
                  'master_data_import_units',
                  'master_data_import_command_outcomes',
                  'master_data_import_takeovers'
              )
            """
        ).fetchall()
    }
    expected_policies = {
        (table_name, policy_name)
        for table_name in (
            "master_data_import_batches",
            "master_data_import_units",
            "master_data_import_command_outcomes",
            "master_data_import_takeovers",
        )
        for policy_name in ("tenant_select", "tenant_insert", "tenant_update", "owner_delete")
    }
    expected_policies.add(("master_data_import_batches", "takeover_definer_tenant_select"))
    expected_policies.add(("master_data_import_units", "takeover_definer_tenant_select"))
    expected_policies.add(("master_data_import_takeovers", "takeover_definer_tenant_select"))
    expected_policies.add(("master_data_import_batches", "school_reactivation_context_select"))
    expected_policies.add(("master_data_import_units", "school_reactivation_context_select"))
    expected_policies.add(("master_data_import_units", "historical_content_reader_select"))
    assert policies == expected_policies

    for table_name in (
        "master_data_import_batches",
        "master_data_import_units",
        "master_data_import_command_outcomes",
        "master_data_import_takeovers",
    ):
        assert connection.execute(
            "SELECT has_table_privilege('campus_ai_runtime', %s, 'SELECT,INSERT')",
            (f"public.{table_name}",),
        ).fetchone() == (True,)
        assert connection.execute(
            "SELECT has_table_privilege('campus_ai_runtime', %s, 'UPDATE,DELETE,TRUNCATE')",
            (f"public.{table_name}",),
        ).fetchone() == (False,)
        expected_update = table_name in {
            "master_data_import_batches",
            "master_data_import_units",
        }
        assert connection.execute(
            "SELECT has_any_column_privilege('campus_ai_runtime', %s, 'UPDATE')",
            (f"public.{table_name}",),
        ).fetchone() == (expected_update,)

    for table_name, immutable_column, mutable_column in (
        ("master_data_import_batches", "source_sha256", "status"),
        ("master_data_import_units", "payload", "status"),
    ):
        assert connection.execute(
            "SELECT has_column_privilege('campus_ai_runtime', %s, %s, 'UPDATE')",
            (f"public.{table_name}", immutable_column),
        ).fetchone() == (False,)
        assert connection.execute(
            "SELECT has_column_privilege('campus_ai_runtime', %s, %s, 'UPDATE')",
            (f"public.{table_name}", mutable_column),
        ).fetchone() == (True,)

    takeover_runtime_privileges = {
        privilege: connection.execute(
            "SELECT has_table_privilege('campus_ai_runtime', "
            "'public.master_data_import_takeovers', %s)",
            (privilege,),
        ).fetchone()
        == (True,)
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
    }
    assert takeover_runtime_privileges == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": False,
        "DELETE": False,
        "TRUNCATE": False,
    }

    takeover_trigger_definitions = {
        row[0]: row[1]
        for row in connection.execute(
            """
            SELECT trigger_row.tgname, pg_get_triggerdef(trigger_row.oid)
            FROM pg_trigger AS trigger_row
            JOIN pg_class AS table_row ON table_row.oid = trigger_row.tgrelid
            JOIN pg_namespace AS schema_row ON schema_row.oid = table_row.relnamespace
            WHERE schema_row.nspname = 'public'
              AND table_row.relname = 'master_data_import_takeovers'
              AND NOT trigger_row.tgisinternal
            ORDER BY trigger_row.tgname
            """
        ).fetchall()
    }
    assert (
        "BEFORE DELETE OR UPDATE"
        in takeover_trigger_definitions["reject_master_data_import_takeover_row_mutation"]
    )
    assert (
        "FOR EACH ROW"
        in takeover_trigger_definitions["reject_master_data_import_takeover_row_mutation"]
    )
    assert (
        "BEFORE TRUNCATE"
        in takeover_trigger_definitions["reject_master_data_import_takeover_truncate"]
    )
    assert (
        "FOR EACH STATEMENT"
        in takeover_trigger_definitions["reject_master_data_import_takeover_truncate"]
    )
    assert connection.execute(
        """
        SELECT owner_role.rolname,
               function_row.prosecdef,
               function_row.proconfig,
               has_function_privilege(
                   'campus_ai_runtime', function_row.oid, 'EXECUTE'
               )
        FROM pg_proc AS function_row
        JOIN pg_namespace AS schema_row ON schema_row.oid = function_row.pronamespace
        JOIN pg_roles AS owner_role ON owner_role.oid = function_row.proowner
        WHERE schema_row.nspname = 'app_private'
          AND function_row.proname = 'reject_master_data_import_takeover_mutation'
        """
    ).fetchone() == (
        "campus_ai_db_owner",
        False,
        ["search_path=pg_catalog"],
        False,
    )


@pytest.mark.integration
def test_takeover_manifest_is_typed_db_owned_and_captured_by_narrow_trigger(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    """转移序号/版本是数据库生成的有类型溯源，runtime 与客户端都没有写入权威。"""

    connection = current_owner_connection
    assert connection.execute(
        """
        SELECT column_name, udt_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'master_data_import_takeovers'
          AND column_name IN ('transferred_sequences', 'transferred_unit_versions')
        ORDER BY column_name
        """
    ).fetchall() == [
        ("transferred_sequences", "_int4", "NO"),
        ("transferred_unit_versions", "_int8", "NO"),
    ]
    constraint_names = {
        row[0]
        for row in connection.execute(
            """
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_schema = 'public'
              AND table_name = 'master_data_import_takeovers'
            """
        ).fetchall()
    }
    assert {
        "ck_master_data_import_takeovers_manifest_cardinality",
        "ck_master_data_import_takeovers_manifest_positive",
    }.issubset(constraint_names)
    assert connection.execute(
        """
        SELECT owner_role.rolname,
               function_row.prosecdef,
               function_row.proconfig,
               has_function_privilege('campus_ai_runtime', function_row.oid, 'EXECUTE'),
               has_function_privilege('public', function_row.oid, 'EXECUTE'),
               pg_get_triggerdef(trigger_row.oid)
        FROM pg_proc AS function_row
        JOIN pg_namespace AS schema_row ON schema_row.oid = function_row.pronamespace
        JOIN pg_roles AS owner_role ON owner_role.oid = function_row.proowner
        JOIN pg_trigger AS trigger_row ON trigger_row.tgfoid = function_row.oid
        WHERE schema_row.nspname = 'app_private'
          AND function_row.proname = 'populate_master_data_import_takeover_manifest'
          AND trigger_row.tgname = 'populate_master_data_import_takeover_manifest'
        """
    ).fetchone() == (
        "campus_ai_takeover_definer",
        True,
        ["search_path=pg_catalog"],
        False,
        False,
        "CREATE TRIGGER populate_master_data_import_takeover_manifest BEFORE INSERT ON "
        "public.master_data_import_takeovers FOR EACH ROW EXECUTE FUNCTION "
        "app_private.populate_master_data_import_takeover_manifest()",
    )


@pytest.mark.integration
def test_takeover_integrity_is_deferred_without_transaction_metadata_authority(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    """attempt/接管完整性由延后事实约束证明，不把事务标识当作业务授权事实。"""

    connection = current_owner_connection
    trigger_rows = connection.execute(
        """
        SELECT trigger_row.tgname,
               table_row.relname,
               trigger_row.tgdeferrable,
               trigger_row.tginitdeferred
        FROM pg_trigger AS trigger_row
        JOIN pg_class AS table_row ON table_row.oid = trigger_row.tgrelid
        JOIN pg_namespace AS schema_row ON schema_row.oid = table_row.relnamespace
        WHERE schema_row.nspname = 'public'
          AND trigger_row.tgname IN (
              'enforce_master_data_import_attempt_transition',
              'enforce_master_data_import_takeover_insert_integrity',
              'enforce_master_data_import_takeover_batch_integrity',
              'enforce_master_data_import_takeover_command_integrity'
          )
        ORDER BY trigger_row.tgname
        """
    ).fetchall()
    assert trigger_rows == [
        ("enforce_master_data_import_attempt_transition", "master_data_import_units", True, True),
        (
            "enforce_master_data_import_takeover_batch_integrity",
            "master_data_import_batches",
            True,
            True,
        ),
        (
            "enforce_master_data_import_takeover_command_integrity",
            "command_idempotency_records",
            True,
            True,
        ),
        (
            "enforce_master_data_import_takeover_insert_integrity",
            "master_data_import_takeovers",
            True,
            True,
        ),
    ]
    function_definitions = "\n".join(
        row[0]
        for row in connection.execute(
            """
            SELECT pg_get_functiondef(function_row.oid)
            FROM pg_proc AS function_row
            JOIN pg_namespace AS schema_row ON schema_row.oid = function_row.pronamespace
            WHERE schema_row.nspname IN ('app', 'app_private')
              AND function_row.proname IN (
                  'supersede_master_data_import_takeover_source',
                  'populate_master_data_import_takeover_manifest',
                  'assert_master_data_import_takeover_integrity',
                  'enforce_master_data_import_attempt_transition',
                  'enforce_master_data_import_takeover_integrity'
              )
            ORDER BY schema_row.nspname, function_row.proname
            """
        ).fetchall()
    ).lower()
    for forbidden_fragment in (
        "xmin",
        "pg_current_xact_id",
        "pg_xact_status",
        "xid8",
    ):
        assert forbidden_fragment not in function_definitions
    audit_definition_row = connection.execute(
        """
        SELECT pg_get_functiondef(
            'app.append_master_data_import_audit(integer,uuid,uuid,uuid,bigint,text,text,'
            'text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)'::regprocedure
        )
        """
    ).fetchone()
    assert audit_definition_row is not None
    audit_definition = audit_definition_row[0].lower()
    for forbidden_fragment in (
        "xmin",
        "pg_current_xact_id",
        "pg_xact_status",
        "xid8",
    ):
        assert forbidden_fragment not in audit_definition
    private_audit = connection.execute(
        """
        SELECT owner_role.rolname,
               has_function_privilege('campus_ai_runtime', function_row.oid, 'EXECUTE'),
               has_function_privilege('campus_ai_db_owner', function_row.oid, 'EXECUTE'),
               has_function_privilege('campus_ai_takeover_definer', function_row.oid, 'EXECUTE'),
               has_function_privilege('public', function_row.oid, 'EXECUTE')
        FROM pg_proc AS function_row
        JOIN pg_namespace AS schema_row ON schema_row.oid = function_row.pronamespace
        JOIN pg_roles AS owner_role ON owner_role.oid = function_row.proowner
        WHERE schema_row.nspname = 'app_private'
          AND function_row.proname = 'append_master_data_import_audit_xid'
        """
    ).fetchone()
    assert private_audit == ("campus_ai_audit_definer", False, False, False, False)


def test_takeover_manifest_model_columns_are_server_fetched() -> None:
    """应用模型只读取数据库清单；构造接管事实时不会向服务器提供权威数组。"""

    models = importlib.import_module("app.platform.master_data_imports.models")
    table = models.MasterDataImportTakeover.__table__
    for column_name in ("transferred_sequences", "transferred_unit_versions"):
        column = table.c[column_name]
        assert column.nullable is False
        assert column.server_default is not None


def _seed_takeover_provenance(
    current_master_data_schema: PostgresRoles,
    import_actor_facts: ImportActorFacts,
) -> UUID:
    """以 Owner 追加一条合法接管事实，返回其稳定标识。"""

    original_actor = import_actor_facts
    new_actor = _seed_same_school_import_actor(
        current_master_data_schema,
        school_id=original_actor.school_id,
    )
    batch_id, previous_record_id, new_record_id, takeover_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    context = _command_context(
        new_actor,
        idempotency_key=f"task8-takeover-provenance-{uuid4().hex}",
    )
    _seed_takeover_prestate(
        current_master_data_schema,
        original_actor=original_actor,
        batch_id=batch_id,
        previous_record_id=previous_record_id,
    )
    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        _insert_takeover_audit_fact_set(
            connection,
            context=context,
            original_actor=original_actor,
            new_actor=new_actor,
            batch_id=batch_id,
            previous_record_id=previous_record_id,
            new_record_id=new_record_id,
            takeover_id=takeover_id,
            takeover_reason="原活动任职已失效，由现任管理员接管",
        )
        _append_takeover_audit(
            connection,
            context=context,
            actor=new_actor,
            batch_id=batch_id,
            takeover_id=takeover_id,
        )
        connection.commit()
    return takeover_id


@pytest.mark.integration
def _retired_legacy_test_runtime_cannot_update_or_delete_takeover_record(
    current_master_data_schema: PostgresRoles,
    import_actor_facts: ImportActorFacts,
) -> None:
    """运行账号对 Owner 追加的接管记录只能读取，不得改写或删除。"""

    takeover_id = _seed_takeover_provenance(
        current_master_data_schema,
        import_actor_facts,
    )

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with current_master_data_schema.runtime_connection() as connection:
            connection.execute(
                "UPDATE master_data_import_takeovers SET takeover_reason = %s WHERE id = %s",
                ("试图改写原因", takeover_id),
            )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with current_master_data_schema.runtime_connection() as connection:
            connection.execute(
                "DELETE FROM master_data_import_takeovers WHERE id = %s",
                (takeover_id,),
            )

    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        assert connection.execute(
            "SELECT takeover_reason FROM master_data_import_takeovers WHERE id = %s",
            (takeover_id,),
        ).fetchone() == ("原活动任职已失效，由现任管理员接管",)


@pytest.mark.integration
@pytest.mark.parametrize("owner_operation", ["update", "delete", "truncate"])
def _retired_legacy_test_owner_cannot_mutate_or_truncate_takeover_provenance(
    current_master_data_schema: PostgresRoles,
    import_actor_facts: ImportActorFacts,
    owner_operation: str,
) -> None:
    """数据库 Owner 的普通 DML 也不能改写、删除或清空接管来源事实。"""

    takeover_id = _seed_takeover_provenance(
        current_master_data_schema,
        import_actor_facts,
    )

    def snapshot() -> tuple[tuple[Any, ...], ...]:
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            return tuple(
                connection.execute(
                    """
                    SELECT school_id, id, to_jsonb(takeover_row)
                    FROM master_data_import_takeovers AS takeover_row
                    ORDER BY school_id, id
                    """
                ).fetchall()
            )

    before = snapshot()
    statements = {
        "update": (
            "UPDATE master_data_import_takeovers SET takeover_reason = %s WHERE id = %s",
            ("Owner 试图改写接管原因", takeover_id),
        ),
        "delete": (
            "DELETE FROM master_data_import_takeovers WHERE id = %s",
            (takeover_id,),
        ),
        "truncate": ("TRUNCATE TABLE master_data_import_takeovers", None),
    }
    statement, parameters = statements[owner_operation]
    with pytest.raises(psycopg.Error) as captured:
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            connection.execute(statement, parameters)
    assert captured.value.sqlstate == "55000"
    assert snapshot() == before


@pytest.mark.integration
def _retired_legacy_test_takeover_record_rejects_same_previous_and_new_command(
    current_master_data_schema: PostgresRoles,
    import_actor_facts: ImportActorFacts,
) -> None:
    """接管来源和新命令不能引用同一条幂等记录。"""

    source_takeover_id = _seed_takeover_provenance(
        current_master_data_schema,
        import_actor_facts,
    )
    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-self-link-takeover-{uuid4().hex}",
    )
    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET LOCAL ROLE campus_ai_runtime")
        set_local_execution_context(cast(Any, connection), _envelope_factory(context))
        connection.execute("RESET ROLE")
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO master_data_import_takeovers (
                    id, school_id, batch_id,
                    previous_idempotency_record_id, new_idempotency_record_id,
                    original_actor_user_id, original_actor_person_id,
                    original_actor_assignment_id, original_actor_assignment_version,
                    new_actor_user_id, new_actor_person_id,
                    new_actor_assignment_id, new_actor_assignment_version,
                    takeover_reason, request_id,
                    batch_version_before, batch_version_after
                )
                SELECT
                    %s, school_id, batch_id,
                    new_idempotency_record_id, new_idempotency_record_id,
                    original_actor_user_id, original_actor_person_id,
                    original_actor_assignment_id, original_actor_assignment_version,
                    new_actor_user_id, new_actor_person_id,
                    new_actor_assignment_id, new_actor_assignment_version,
                    '拒绝自引用接管', %s, 2, 3
                FROM master_data_import_takeovers
                WHERE id = %s
                """,
                (
                    uuid4(),
                    uuid4(),
                    source_takeover_id,
                ),
            )


@pytest.mark.integration
def _retired_legacy_test_takeover_record_action_constraints_allow_only_declared_action(
    current_master_data_schema: PostgresRoles,
    import_actor_facts: ImportActorFacts,
) -> None:
    """批次和结果允许固定接管动作，但数据库继续拒绝任意动作代码。"""

    takeover_id = _seed_takeover_provenance(
        current_master_data_schema,
        import_actor_facts,
    )
    outcome_id = uuid4()
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        facts = connection.execute(
            """
            SELECT school_id, batch_id, new_idempotency_record_id
            FROM master_data_import_takeovers
            WHERE id = %s
            """,
            (takeover_id,),
        ).fetchone()
        assert facts is not None
        school_id, batch_id, record_id = facts
        connection.execute(
            """
            INSERT INTO master_data_import_command_outcomes (
                id, school_id, idempotency_record_id, batch_id, action_code,
                status, batch_version, succeeded_units, failed_units, executed_units
            ) VALUES (
                %s, %s, %s, %s, 'master_data.import.ordinary_takeover',
                'applied', 1, ARRAY[]::integer[], ARRAY[]::integer[], ARRAY[]::integer[]
            )
            """,
            (outcome_id, school_id, record_id, batch_id),
        )
        snapshot = connection.execute(
            "SELECT unit_outcome_snapshots FROM master_data_import_command_outcomes WHERE id = %s",
            (outcome_id,),
        ).fetchone()
        assert snapshot == ([],)
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            connection.execute(
                """
                INSERT INTO master_data_import_command_outcomes (
                    id, school_id, idempotency_record_id, batch_id, action_code,
                    status, batch_version, succeeded_units, failed_units, executed_units,
                    unit_outcome_snapshots
                ) VALUES (
                    %s, %s, %s, %s, 'master_data.import.ordinary_takeover',
                    'applied', 1, '{}'::integer[], '{}'::integer[], '{}'::integer[],
                    '[]'::jsonb
                )
                """,
                (uuid4(), school_id, record_id, batch_id),
            )

        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "UPDATE master_data_import_batches SET active_action_code = %s WHERE id = %s",
                ("master_data.import.arbitrary", batch_id),
            )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "UPDATE master_data_import_command_outcomes SET action_code = %s WHERE id = %s",
                ("master_data.import.arbitrary", outcome_id),
            )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "DELETE FROM master_data_import_command_outcomes WHERE id = %s",
                (outcome_id,),
            )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute("TRUNCATE master_data_import_command_outcomes")


@pytest.mark.integration
def test_takeover_idempotency_repository_reads_owner_and_cas_terminal_failure(
    current_owner_session: Session,
    import_actor_facts: ImportActorFacts,
) -> None:
    """按记录标识读取只返回窄归属投影，旧命令终态只能精确 CAS 一次。"""

    idempotency_contracts = importlib.import_module("app.platform.idempotency.contracts")
    idempotency_repository = importlib.import_module("app.platform.idempotency.repository")
    importlib.import_module("app.domains.students.models")
    importlib.import_module("app.platform.directory.models")
    importlib.import_module("app.platform.identity.models")
    importlib.import_module("app.platform.organization.models")
    importlib.import_module("app.platform.tenancy.models")
    request_sha256 = "6" * 64
    resource_id = str(uuid4())
    identity = idempotency_contracts.CommandIdempotencyIdentity(
        school_id=import_actor_facts.school_id,
        action_code="master_data.import.ordinary_resume",
        actor_user_id=import_actor_facts.actor_user_id,
        actor_person_id=import_actor_facts.actor_person_id,
        actor_context_type="assignment",
        actor_assignment_id=import_actor_facts.actor_assignment_id,
        actor_assignment_version=1,
        actor_guardian_relationship_id=None,
        actor_guardian_relationship_version=None,
        resource_type="master_data.import_batch",
        resource_id=resource_id,
        idempotency_key_hash="7" * 64,
        request_sha256=request_sha256,
    )
    repository = idempotency_repository.SqlAlchemyCommandIdempotencyRepository(
        current_owner_session
    )
    acquired = repository.acquire(identity)
    record_id = acquired.record_id

    projection = repository.find_by_record_id(import_actor_facts.school_id, record_id)
    assert projection is not None
    assert projection.record_id == record_id
    assert projection.action_code == "master_data.import.ordinary_resume"
    assert projection.actor_assignment_id == import_actor_facts.actor_assignment_id
    assert projection.resource_id == resource_id
    assert not hasattr(projection, "result_payload")
    assert repository.lock_by_record_id(import_actor_facts.school_id, record_id) == projection
    other_school_id = SchoolId(uuid4())
    assert repository.find_by_record_id(other_school_id, record_id) is None
    assert repository.lock_by_record_id(other_school_id, record_id) is None

    result = idempotency_contracts.SafeCommandResult(
        resource_type="master_data.import_batch",
        resource_id=UUID(resource_id),
        resource_version=2,
        outcome_code="master_data.import.command_superseded",
    )
    with pytest.raises(idempotency_contracts.IdempotencyPayloadConflict):
        repository.mark_record_terminal_failure(
            other_school_id,
            record_id,
            expected_request_sha256=request_sha256,
            result_type="master_data.import_outcome",
            result=result,
        )
    terminal = repository.mark_record_terminal_failure(
        import_actor_facts.school_id,
        record_id,
        expected_request_sha256=request_sha256,
        result_type="master_data.import_outcome",
        result=result,
    )
    assert terminal.status == "terminal_failure"
    assert terminal.result == result
    assert (
        repository.mark_record_terminal_failure(
            import_actor_facts.school_id,
            record_id,
            expected_request_sha256=request_sha256,
            result_type="master_data.import_outcome",
            result=result,
        )
        == terminal
    )

    with pytest.raises(idempotency_contracts.IdempotencyPayloadConflict):
        repository.mark_record_terminal_failure(
            import_actor_facts.school_id,
            record_id,
            expected_request_sha256=request_sha256,
            result_type="master_data.import_outcome",
            result=result.model_copy(update={"resource_version": 3}),
        )


@pytest.mark.integration
def _retired_legacy_test_takeover_repository_rejects_unproven_attempt_lineage(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """恢复 Reader 不含当前命令，只按接管时间从最早到最近返回 previous 链。"""

    models = importlib.import_module("app.platform.master_data_imports.models")
    repository_module = importlib.import_module("app.platform.master_data_imports.repository")
    importlib.import_module("app.domains.students.models")
    importlib.import_module("app.platform.directory.models")
    importlib.import_module("app.platform.identity.models")
    importlib.import_module("app.platform.organization.models")
    importlib.import_module("app.platform.tenancy.models")
    batch_id = uuid4()
    record_ids: list[UUID] = []
    batch = models.MasterDataImportBatch(
        id=batch_id,
        school_id=import_actor_facts.school_id,
        mode="ordinary_atomic_units",
        source_kind="excel",
        source_ref=f"protected-{uuid4().hex}",
        source_sha256="8" * 64,
        filename_summary="takeover-lineage.xlsx",
        source_size_bytes=1024,
        worksheet_names=[],
        mapping_version=None,
        mapping_payload=None,
        status="mapped",
        planned_units=1,
        succeeded_units=0,
        failed_units=0,
        pending_units=1,
        confirmation_card_sha256=None,
        confirmed_by_user_id=None,
        confirmed_by_person_id=None,
        confirmed_by_assignment_id=None,
        confirmed_at=None,
        request_id=uuid4(),
        idempotency_key_hash="9" * 64,
        active_idempotency_record_id=None,
        active_action_code=None,
        version=3,
    )
    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-takeover-lineage-{uuid4().hex}",
    )
    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO master_data_import_batches (
                id, school_id, mode, source_kind, source_ref, source_sha256,
                filename_summary, source_size_bytes, worksheet_names, status,
                planned_units, succeeded_units, failed_units, pending_units,
                request_id, idempotency_key_hash, version
            ) VALUES (
                %s, %s, 'ordinary_atomic_units', 'excel', %s, %s,
                'takeover-lineage.xlsx', 1024, '[]'::jsonb, 'mapped',
                1, 0, 0, 1, %s, %s, 3
            )
            """,
            (
                batch_id,
                import_actor_facts.school_id,
                batch.source_ref,
                batch.source_sha256,
                batch.request_id,
                batch.idempotency_key_hash,
            ),
        )
        for index in range(3):
            record_id = uuid4()
            record_ids.append(record_id)
            connection.execute(
                """
                INSERT INTO command_idempotency_records (
                    id, school_id, action_code, actor_user_id, actor_person_id,
                    actor_context_type, actor_context_key, actor_assignment_id,
                    actor_assignment_version, resource_type, resource_id,
                    idempotency_key_hash, request_sha256, status
                ) VALUES (
                    %s, %s, 'master_data.import.ordinary_resume', %s, %s,
                    'assignment', %s, %s, 1, 'master_data.import_batch', %s,
                    %s, %s, 'in_progress'
                )
                """,
                (
                    record_id,
                    import_actor_facts.school_id,
                    import_actor_facts.actor_user_id,
                    import_actor_facts.actor_person_id,
                    f"assignment:{import_actor_facts.actor_assignment_id}:1",
                    import_actor_facts.actor_assignment_id,
                    str(batch_id),
                    f"{index + 1}" * 64,
                    f"{index + 4}" * 64,
                ),
            )
        connection.execute(
            """
            INSERT INTO master_data_import_units (
                id, school_id, batch_id, sequence, row_number, unit_kind, status,
                payload, payload_sha256, soft_anomalies, initial_soft_anomalies,
                last_attempt_idempotency_record_id, last_attempt_completed, version
            ) VALUES (
                %s, %s, %s, 1, 1, 'person', 'ready', '{}'::jsonb, %s,
                '[]'::jsonb, '[]'::jsonb, %s, false, 1
            )
            """,
            (uuid4(), import_actor_facts.school_id, batch_id, "a" * 64, record_ids[0]),
        )
        connection.commit()
    _, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    takeover_ids = (uuid4(), uuid4())
    created_at = (
        datetime(2026, 8, 8, 8, 0, tzinfo=UTC),
        datetime(2026, 8, 8, 9, 0, tzinfo=UTC),
    )
    with uow_factory.open(context) as uow:
        for index, (previous_id, new_id) in enumerate(
            zip(record_ids, record_ids[1:], strict=False)
        ):
            uow.imports.add_takeover(
                models.MasterDataImportTakeover(
                    id=takeover_ids[index],
                    school_id=import_actor_facts.school_id,
                    batch_id=batch_id,
                    previous_idempotency_record_id=previous_id,
                    new_idempotency_record_id=new_id,
                    original_actor_user_id=import_actor_facts.actor_user_id,
                    original_actor_person_id=import_actor_facts.actor_person_id,
                    original_actor_assignment_id=import_actor_facts.actor_assignment_id,
                    original_actor_assignment_version=1,
                    new_actor_user_id=import_actor_facts.actor_user_id,
                    new_actor_person_id=import_actor_facts.actor_person_id,
                    new_actor_assignment_id=import_actor_facts.actor_assignment_id,
                    new_actor_assignment_version=1,
                    takeover_reason=f"接管链测试 {index + 1}",
                    request_id=uuid4(),
                    batch_version_before=index + 1,
                    batch_version_after=index + 2,
                    created_at=created_at[index],
                )
            )
            if index == 0:
                unit = uow.imports.lock_unit(import_actor_facts.school_id, batch_id, 1)
                assert unit is not None
                unit.last_attempt_idempotency_record_id = record_ids[1]
                uow.imports.save_unit(unit)

        repository = repository_module.SqlAlchemyMasterDataImportRepository(uow.session)
        found_batch = repository.find_batch(import_actor_facts.school_id, batch_id)
        assert found_batch is not None and found_batch.id == batch.id
        assert tuple(
            unit.sequence for unit in repository.find_units(import_actor_facts.school_id, batch_id)
        ) == (1,)
        other_school_id = SchoolId(uuid4())
        assert repository.find_batch(other_school_id, batch_id) is None
        assert repository.find_units(other_school_id, batch_id) == ()
        assert (
            repository.find_takeover_by_new_record(
                import_actor_facts.school_id,
                record_ids[1],
            ).id
            == takeover_ids[0]
        )
        with pytest.raises(DBAPIError):
            with uow.session.begin_nested():
                repository.find_attempt_lineage(
                    import_actor_facts.school_id,
                    record_ids[2],
                )
        assert (
            repository.lock_takeover_by_previous_record(
                import_actor_facts.school_id,
                record_ids[0],
            ).id
            == takeover_ids[0]
        )
        with pytest.raises(DBAPIError):
            with uow.session.begin_nested():
                repository.find_attempt_lineage(other_school_id, record_ids[2])


@pytest.mark.integration
def _retired_legacy_test_previous_record_advisory_key_blocks_only_same_key_on_real_connections(
    current_master_data_schema: PostgresRoles,
    import_actor_facts: ImportActorFacts,
) -> None:
    """同 key 等待事务提交后读到 winner；异 key 在锁等待预算内立即独立通过。"""

    repository_module = importlib.import_module("app.platform.master_data_imports.repository")
    takeover_id = _seed_takeover_provenance(
        current_master_data_schema,
        import_actor_facts,
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        key_row = connection.execute(
            """
            SELECT school_id, previous_idempotency_record_id
            FROM master_data_import_takeovers
            WHERE id = %s
            """,
            (takeover_id,),
        ).fetchone()
    assert key_row is not None
    school_id = SchoolId(key_row[0])
    previous_record_id = key_row[1]

    migration = current_master_data_schema.migration_connection_parameters
    engine = create_engine(
        URL.create(
            drivername="postgresql+psycopg",
            username=str(migration["user"]),
            password=str(migration["password"]),
            host=str(migration["host"]),
            port=int(migration["port"]),
            database=str(migration["dbname"]),
        ),
        poolclass=NullPool,
    )
    holder = Session(bind=engine, autoflush=False)
    try:
        holder.execute(text("SET ROLE campus_ai_db_owner"))
        holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
        assert isinstance(holder_pid, int)
        holder_repository = repository_module.SqlAlchemyMasterDataImportRepository(holder)
        held_takeover = holder_repository.lock_takeover_by_previous_record(
            school_id,
            previous_record_id,
        )
        assert held_takeover is not None
        assert held_takeover.id == takeover_id

        def lock_from_new_connection(
            requested_record_id: UUID,
            *,
            start: Barrier | None = None,
            pid_slot: dict[str, int] | None = None,
        ) -> tuple[int, UUID | None]:
            with Session(bind=engine, autoflush=False) as contender:
                contender.execute(text("SET ROLE campus_ai_db_owner"))
                contender.execute(text("SET LOCAL lock_timeout = '5s'"))
                contender_pid = contender.scalar(text("SELECT pg_backend_pid()"))
                assert isinstance(contender_pid, int)
                if pid_slot is not None:
                    pid_slot["pid"] = contender_pid
                if start is not None:
                    start.wait(timeout=3)
                contender_repository = repository_module.SqlAlchemyMasterDataImportRepository(
                    contender
                )
                found = contender_repository.lock_takeover_by_previous_record(
                    school_id,
                    requested_record_id,
                )
                found_id = found.id if found is not None else None
                contender.commit()
                return contender_pid, found_id

        with ThreadPoolExecutor(max_workers=1) as executor:
            different_future = executor.submit(
                lock_from_new_connection,
                uuid4(),
            )
            different_pid, different_takeover_id = different_future.result(timeout=2)
        assert different_pid != holder_pid
        assert different_takeover_id is None

        start = Barrier(2)
        same_pid_slot: dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=1) as executor:
            same_future = executor.submit(
                lock_from_new_connection,
                previous_record_id,
                start=start,
                pid_slot=same_pid_slot,
            )
            start.wait(timeout=3)
            same_pid = same_pid_slot["pid"]
            assert same_pid != holder_pid
            blocked = False
            deadline = monotonic() + 2
            with current_master_data_schema.admin_connection() as observer:
                while monotonic() < deadline:
                    wait_row = observer.execute(
                        """
                        SELECT wait_event_type, wait_event, pg_blocking_pids(%s)
                        FROM pg_stat_activity
                        WHERE pid = %s
                        """,
                        (same_pid, same_pid),
                    ).fetchone()
                    if (
                        wait_row is not None
                        and wait_row[0] == "Lock"
                        and wait_row[1] == "advisory"
                        and holder_pid in wait_row[2]
                    ):
                        blocked = True
                        break
            assert blocked is True
            holder.commit()
            contender_pid, contender_takeover_id = same_future.result(timeout=3)
        assert contender_pid == same_pid
        assert contender_takeover_id == takeover_id
    finally:
        holder.rollback()
        holder.close()
        engine.dispose()


@pytest.mark.integration
def _retired_legacy_test_takeover_repository_lineage_rejects_cycle_as_incomplete_provenance(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """固定恢复边界拒绝两点环，不能把截断 previous 集合误当成完整 lineage。"""

    source_takeover_id = _seed_takeover_provenance(
        current_master_data_schema,
        import_actor_facts,
    )
    cycle_record_ids = (uuid4(), uuid4())
    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-cycle-takeover-{uuid4().hex}",
    )
    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        for index, record_id in enumerate(cycle_record_ids):
            connection.execute(
                """
                INSERT INTO command_idempotency_records (
                    id, school_id, action_code, actor_user_id, actor_person_id,
                    actor_context_type, actor_context_key, actor_assignment_id,
                    actor_assignment_version, resource_type, resource_id,
                    idempotency_key_hash, request_sha256, status
                )
                SELECT
                    %s, school_id, 'master_data.import.ordinary_resume',
                    new_actor_user_id, new_actor_person_id, 'assignment',
                    'assignment:' || new_actor_assignment_id::text || ':' ||
                        new_actor_assignment_version::text,
                    new_actor_assignment_id, new_actor_assignment_version,
                    'master_data.import_batch', batch_id::text, %s, %s, 'in_progress'
                FROM master_data_import_takeovers
                WHERE id = %s
                """,
                (
                    record_id,
                    f"{index + 6}" * 64,
                    f"{index + 8}" * 64,
                    source_takeover_id,
                ),
            )
        source_facts = connection.execute(
            """
            SELECT school_id, batch_id,
                   new_actor_user_id, new_actor_person_id,
                   new_actor_assignment_id, new_actor_assignment_version
            FROM master_data_import_takeovers
            WHERE id = %s
            """,
            (source_takeover_id,),
        ).fetchone()
        connection.commit()
    assert source_facts is not None
    school_id, batch_id, actor_user_id, actor_person_id, assignment_id, assignment_version = (
        source_facts
    )
    models = importlib.import_module("app.platform.master_data_imports.models")
    repository_module = importlib.import_module("app.platform.master_data_imports.repository")
    _, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    with uow_factory.open(context) as uow:
        unit = uow.imports.lock_unit(school_id, batch_id, 1)
        assert unit is not None
        unit.last_attempt_idempotency_record_id = cycle_record_ids[0]
        uow.imports.save_unit(unit)
        for index, (previous_id, new_id) in enumerate(
            (
                (cycle_record_ids[0], cycle_record_ids[1]),
                (cycle_record_ids[1], cycle_record_ids[0]),
            )
        ):
            uow.imports.add_takeover(
                models.MasterDataImportTakeover(
                    id=uuid4(),
                    school_id=school_id,
                    batch_id=batch_id,
                    previous_idempotency_record_id=previous_id,
                    new_idempotency_record_id=new_id,
                    original_actor_user_id=actor_user_id,
                    original_actor_person_id=actor_person_id,
                    original_actor_assignment_id=assignment_id,
                    original_actor_assignment_version=assignment_version,
                    new_actor_user_id=actor_user_id,
                    new_actor_person_id=actor_person_id,
                    new_actor_assignment_id=assignment_id,
                    new_actor_assignment_version=assignment_version,
                    takeover_reason=f"历史环接管 {index + 1}",
                    request_id=uuid4(),
                    batch_version_before=index + 10,
                    batch_version_after=index + 11,
                    created_at=datetime(2026, 8, 8, 10 + index, 0, tzinfo=UTC),
                )
            )
            if index == 0:
                unit.last_attempt_idempotency_record_id = cycle_record_ids[1]
                uow.imports.save_unit(unit)

        reader = repository_module.SqlAlchemyMasterDataImportRecoveryReader(uow.session)
        with pytest.raises(DBAPIError):
            reader.find_attempt_lineage(
                import_actor_facts.school_id,
                cycle_record_ids[1],
            )


@pytest.mark.integration
def _retired_legacy_test_runtime_cannot_change_confirmed_preview_payload(
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """运行账号不能在确认卡生成后改写候选载荷并沿用旧摘要。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    uow_factory = application.SqlAlchemyMasterDataImportUoWFactory(
        session_factory=import_runtime_session_factory,
        envelope_factory=_envelope_factory,
        lock_timeout_ms=750,
    )
    service = application.MasterDataImportService(
        uow_factory=uow_factory,
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-immutable-payload-{uuid4().hex}",
    )
    preview = service.stage_manual(
        contracts.StageManualPersonUnitCommand(
            source_reference=f"manual-{uuid4().hex}",
            unit=contracts.PersonImportUnitDraft(
                row_number=1,
                display_name="不可变草稿教师",
                external_reference=f"T-{uuid4().hex}",
                valid_from=datetime.now(UTC).replace(microsecond=0),
            ),
        ),
        context=context,
    )

    with uow_factory.open(context) as uow:
        with pytest.raises(ProgrammingError):
            uow.session.execute(
                text(
                    """
                    UPDATE master_data_import_units
                    SET payload = '{}'::jsonb
                    WHERE school_id = :school_id AND batch_id = :batch_id
                    """
                ),
                {
                    "school_id": import_actor_facts.school_id,
                    "batch_id": preview.batch_id,
                },
            )


@pytest.mark.integration
def _retired_legacy_test_exact_outcome_is_read_only_and_cross_school_invisible(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    other_import_actor_facts: ImportActorFacts,
) -> None:
    """首次结果允许本校精确读取，但运行账号不能改删，其他学校看不到。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    uow_factory = application.SqlAlchemyMasterDataImportUoWFactory(
        session_factory=import_runtime_session_factory,
        envelope_factory=_envelope_factory,
        lock_timeout_ms=750,
    )
    service = application.MasterDataImportService(
        uow_factory=uow_factory,
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    staged = service.stage_manual(
        contracts.StageManualPersonUnitCommand(
            source_reference=f"manual-outcome-acl-{marker}",
            unit=contracts.PersonImportUnitDraft(
                row_number=1,
                display_name="结果权限教师",
                external_reference=f"outcome-acl-{marker}",
                valid_from=datetime.now(UTC).replace(microsecond=0),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-outcome-acl-{marker}",
        ),
    )
    service.confirm(
        contracts.ConfirmImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            confirmation_card_sha256=staged.confirmation_card_sha256,
            decisions=(),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-confirm-outcome-acl-{marker}",
        ),
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        outcome_row = connection.execute(
            "SELECT id FROM master_data_import_command_outcomes "
            "WHERE school_id = %s AND batch_id = %s",
            (import_actor_facts.school_id, staged.batch_id),
        ).fetchone()
    assert outcome_row is not None
    outcome_id = outcome_row[0]
    for statement in (
        "UPDATE master_data_import_command_outcomes SET status = 'published' "
        "WHERE school_id = :school_id AND id = :outcome_id",
        "DELETE FROM master_data_import_command_outcomes "
        "WHERE school_id = :school_id AND id = :outcome_id",
    ):
        with uow_factory.open(
            _command_context(
                import_actor_facts,
                idempotency_key=f"task8-mutate-outcome-{uuid4().hex}",
            )
        ) as uow:
            with pytest.raises(ProgrammingError):
                uow.session.execute(
                    text(statement),
                    {
                        "school_id": import_actor_facts.school_id,
                        "outcome_id": outcome_id,
                    },
                )
    with uow_factory.open(
        _command_context(
            other_import_actor_facts,
            idempotency_key=f"task8-cross-school-outcome-{marker}",
        )
    ) as uow:
        visible = uow.session.scalar(
            text("SELECT count(*) FROM master_data_import_command_outcomes WHERE id = :outcome_id"),
            {"outcome_id": outcome_id},
        )
    assert visible == 0


@pytest.mark.integration
@pytest.mark.parametrize("malformation", ("null_state", "null_version", "wrong_target"))
def test_import_audit_function_rejects_null_semantics_and_wrong_stable_target(
    malformation: str,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """绕过应用契约直调窄函数时，SQL NULL 和错误稳定引用都必须失败关闭。"""

    application = importlib.import_module("app.platform.master_data_imports.application")
    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-audit-malformed-{malformation}-{uuid4().hex}",
    )
    uow_factory = application.SqlAlchemyMasterDataImportUoWFactory(
        session_factory=import_runtime_session_factory,
        envelope_factory=_envelope_factory,
        lock_timeout_ms=750,
    )
    target_id = uuid4()
    before_summary: dict[str, object] = {
        "state_code": "person.absent",
        "resource_version": None,
        "stable_reference_ids": [],
        "fact_codes": [],
    }
    after_summary: dict[str, object] = {
        "state_code": "person.confirmed",
        "resource_version": 1,
        "stable_reference_ids": [str(target_id)],
        "fact_codes": ["person.imported"],
    }
    if malformation == "null_state":
        after_summary["state_code"] = None
    elif malformation == "null_version":
        after_summary["resource_version"] = None
    else:
        after_summary["stable_reference_ids"] = [str(uuid4())]
    statement = text(
        """
        SELECT app.append_master_data_import_audit(
            :actor_user_id, :school_id, :actor_person_id,
            :actor_assignment_id, :actor_assignment_version,
            :idempotency_key_hash, 'master_data.person.imported',
            'school_person', :target_id, :batch_id, :unit_id,
            :before_summary, :after_summary,
            :request_id, 'manual.person-import-confirmed'
        )
        """
    ).bindparams(
        bindparam("before_summary", type_=JSONB),
        bindparam("after_summary", type_=JSONB),
    )
    with uow_factory.open(context) as uow:
        with pytest.raises(DBAPIError, match="unsafe master data import audit record"):
            uow.session.execute(
                statement,
                {
                    "actor_user_id": import_actor_facts.actor_user_id,
                    "school_id": import_actor_facts.school_id,
                    "actor_person_id": import_actor_facts.actor_person_id,
                    "actor_assignment_id": import_actor_facts.actor_assignment_id,
                    "actor_assignment_version": 1,
                    "idempotency_key_hash": sha256(
                        context.idempotency_key.encode("utf-8")
                    ).hexdigest(),
                    "target_id": target_id,
                    "batch_id": uuid4(),
                    "unit_id": uuid4(),
                    "before_summary": before_summary,
                    "after_summary": after_summary,
                    "request_id": context.request_id,
                },
            )


@pytest.mark.integration
@pytest.mark.parametrize(
    "violation",
    (
        "missing_target",
        "wrong_batch",
        "wrong_unit",
        "wrong_request",
        "unauthorized_assignment",
        "revoked_assignment",
        "cross_school_target",
    ),
)
def _retired_legacy_test_audit_function_rejects_unbound_import_fact_and_assignment(
    violation: str,
    terminal_manual_import: TerminalImportFacts,
    import_actor_facts: ImportActorFacts,
    other_import_actor_facts: ImportActorFacts,
) -> None:
    """批次、单元、请求、目标或当前授权不实时必须零写入。"""

    with terminal_manual_import.roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        fact_row = connection.execute(
            """
            SELECT unit.id, unit.target_id
            FROM master_data_import_units AS unit
            WHERE unit.school_id = %s AND unit.batch_id = %s
            """,
            (terminal_manual_import.school_id, terminal_manual_import.batch_id),
        ).fetchone()
    assert fact_row is not None
    unit_id, target_id = fact_row
    batch_id = terminal_manual_import.batch_id
    context = terminal_manual_import.context
    if violation == "missing_target":
        target_id = uuid4()
    elif violation == "wrong_batch":
        batch_id = uuid4()
    elif violation == "wrong_unit":
        unit_id = uuid4()
    elif violation == "wrong_request":
        context = _command_context(
            import_actor_facts,
            idempotency_key=f"task8-audit-other-request-{uuid4().hex}",
        )
    elif violation == "cross_school_target":
        target_id = _seed_confirmed_person_name(
            terminal_manual_import.roles,
            school_id=other_import_actor_facts.school_id,
            display_name="跨校审计目标",
            external_reference=f"cross-school-audit-{uuid4().hex}",
        )

    before = terminal_manual_import.snapshot()
    rejected = False
    try:
        with terminal_manual_import.uow_factory.open(context) as uow:
            if violation in {"unauthorized_assignment", "revoked_assignment"}:
                with terminal_manual_import.roles.migration_connection() as connection:
                    connection.execute("SET ROLE campus_ai_db_owner")
                    if violation == "unauthorized_assignment":
                        connection.execute(
                            "UPDATE staff_assignments SET semantic_role_code = 'teacher' "
                            "WHERE school_id = %s AND id = %s",
                            (
                                import_actor_facts.school_id,
                                import_actor_facts.actor_assignment_id,
                            ),
                        )
                        connection.execute(
                            "UPDATE staff_assignment_versions "
                            "SET semantic_role_code = 'teacher' "
                            "WHERE school_id = %s AND assignment_id = %s",
                            (
                                import_actor_facts.school_id,
                                import_actor_facts.actor_assignment_id,
                            ),
                        )
                    else:
                        connection.execute(
                            "UPDATE staff_assignments SET status = 'rejected' "
                            "WHERE school_id = %s AND id = %s",
                            (
                                import_actor_facts.school_id,
                                import_actor_facts.actor_assignment_id,
                            ),
                        )
                        connection.execute(
                            "UPDATE staff_assignment_versions "
                            "SET revoked_at = clock_timestamp() "
                            "WHERE school_id = %s AND assignment_id = %s "
                            "AND revoked_at IS NULL",
                            (
                                import_actor_facts.school_id,
                                import_actor_facts.actor_assignment_id,
                            ),
                        )
                    connection.commit()
                before = terminal_manual_import.snapshot()
            uow.session.execute(
                text(
                    """
                    SELECT app.append_master_data_import_audit(
                        :actor_user_id, :school_id, :actor_person_id,
                        :actor_assignment_id, :actor_assignment_version,
                        :idempotency_key_hash, 'master_data.person.imported',
                        'school_person', :target_id, :batch_id, :unit_id,
                        :before_summary, :after_summary,
                        :request_id, 'manual.person-import-confirmed'
                    )
                    """
                ).bindparams(
                    bindparam("before_summary", type_=JSONB),
                    bindparam("after_summary", type_=JSONB),
                ),
                {
                    "actor_user_id": import_actor_facts.actor_user_id,
                    "school_id": import_actor_facts.school_id,
                    "actor_person_id": import_actor_facts.actor_person_id,
                    "actor_assignment_id": import_actor_facts.actor_assignment_id,
                    "actor_assignment_version": 1,
                    "idempotency_key_hash": sha256(
                        context.idempotency_key.encode("utf-8")
                    ).hexdigest(),
                    "target_id": target_id,
                    "batch_id": batch_id,
                    "unit_id": unit_id,
                    "before_summary": {
                        "state_code": "person.absent",
                        "resource_version": None,
                        "stable_reference_ids": [],
                        "fact_codes": [],
                    },
                    "after_summary": {
                        "state_code": "person.confirmed",
                        "resource_version": 1,
                        "stable_reference_ids": [str(target_id)],
                        "fact_codes": ["person.imported"],
                    },
                    "request_id": context.request_id,
                },
            ).scalar_one()
            uow.commit()
    except DBAPIError as exc:
        expected_error = (
            "master data import audit assignment is not authorized"
            if violation in {"unauthorized_assignment", "revoked_assignment"}
            else "master data import audit fact mismatch"
        )
        assert expected_error in str(exc)
        rejected = True

    assert rejected, f"数据库必须拒绝不绑定真实导入事实的审计：{violation}"
    assert terminal_manual_import.snapshot() == before


@pytest.mark.integration
@pytest.mark.parametrize(
    (
        "actual_same_name_continuation",
        "claim_same_name_continuation",
        "half_consistent_same_name_state",
    ),
    (
        (False, True, None),
        (True, False, None),
        (False, False, "anomaly_without_reason"),
        (False, False, "reason_without_anomaly"),
    ),
)
def test_audit_function_rejects_false_same_name_continuation_summary(
    actual_same_name_continuation: bool,
    claim_same_name_continuation: bool,
    half_consistent_same_name_state: str | None,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """The fixed fact code must exactly reflect anomaly and nonblank decision facts."""

    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-audit-same-name-false-{uuid4().hex}",
    )
    person_id = _seed_confirmed_person_name(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
        display_name=f"Same Name Audit {uuid4().hex}",
        external_reference=f"same-name-audit-{uuid4().hex}",
    )
    batch_id = uuid4()
    unit_id = uuid4()
    _, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    facts = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=context,
        school_id=import_actor_facts.school_id,
        batch_id=batch_id,
    )
    before = facts.snapshot()
    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        try:
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="master data import audit fact mismatch",
            ):
                _insert_manual_audit_fact_set(
                    connection,
                    context=context,
                    actor=import_actor_facts,
                    person_id=person_id,
                    batch_id=batch_id,
                    unit_id=unit_id,
                    record_id=uuid4(),
                    actual_same_name_continuation=actual_same_name_continuation,
                    half_consistent_same_name_state=half_consistent_same_name_state,
                )
                _append_manual_audit(
                    connection,
                    context=context,
                    actor=import_actor_facts,
                    person_id=person_id,
                    batch_id=batch_id,
                    unit_id=unit_id,
                    claim_same_name_continuation=claim_same_name_continuation,
                )
        finally:
            connection.rollback()
    assert facts.snapshot() == before


@pytest.mark.integration
@pytest.mark.parametrize(
    "violation",
    ("wrong_batch", "wrong_unit", "wrong_target", "wrong_request", "wrong_idempotency"),
)
def test_audit_function_rejects_false_manual_facts_created_in_same_transaction(
    violation: str,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """Each manual guard rejects a current-transaction fact mismatch, not merely old xmin."""

    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-audit-manual-same-xact-{violation}-{uuid4().hex}",
    )
    person_id = _seed_confirmed_person_name(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
        display_name=f"Manual Guard {uuid4().hex}",
        external_reference=f"manual-guard-{uuid4().hex}",
    )
    batch_id = uuid4()
    unit_id = uuid4()
    _, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    facts = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=context,
        school_id=import_actor_facts.school_id,
        batch_id=batch_id,
    )
    before = facts.snapshot()
    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        try:
            _insert_manual_audit_fact_set(
                connection,
                context=context,
                actor=import_actor_facts,
                person_id=person_id,
                batch_id=batch_id,
                unit_id=unit_id,
                record_id=uuid4(),
                actual_same_name_continuation=False,
                record_idempotency_key_hash=(
                    "0" * 64 if violation == "wrong_idempotency" else None
                ),
            )
            expected_error = (
                "master data import audit context mismatch"
                if violation == "wrong_request"
                else "master data import audit fact mismatch"
            )
            with pytest.raises(psycopg.Error, match=expected_error):
                _append_manual_audit(
                    connection,
                    context=context,
                    actor=import_actor_facts,
                    person_id=person_id,
                    batch_id=batch_id,
                    unit_id=unit_id,
                    claim_same_name_continuation=False,
                    audit_person_id=uuid4() if violation == "wrong_target" else None,
                    audit_batch_id=uuid4() if violation == "wrong_batch" else None,
                    audit_unit_id=uuid4() if violation == "wrong_unit" else None,
                    audit_request_id=uuid4() if violation == "wrong_request" else None,
                )
        finally:
            connection.rollback()
    assert facts.snapshot() == before


@pytest.mark.integration
def test_audit_function_rejects_version_two_person_facts_in_same_transaction(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """A current v2 person/unit cannot support the fixed version-1 audit summary."""

    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-audit-person-v2-same-xact-{uuid4().hex}",
    )
    person_id = _seed_confirmed_person_name(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
        display_name=f"Version Two Audit {uuid4().hex}",
        external_reference=f"audit-person-v2-same-xact-{uuid4().hex}",
    )
    batch_id = uuid4()
    unit_id = uuid4()
    _, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    facts = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=context,
        school_id=import_actor_facts.school_id,
        batch_id=batch_id,
    )
    before = facts.snapshot()
    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        try:
            _insert_manual_audit_fact_set(
                connection,
                context=context,
                actor=import_actor_facts,
                person_id=person_id,
                batch_id=batch_id,
                unit_id=unit_id,
                record_id=uuid4(),
                actual_same_name_continuation=False,
                person_version=2,
            )
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="master data import audit fact mismatch",
            ):
                _append_manual_audit(
                    connection,
                    context=context,
                    actor=import_actor_facts,
                    person_id=person_id,
                    batch_id=batch_id,
                    unit_id=unit_id,
                    claim_same_name_continuation=False,
                )
        finally:
            connection.rollback()
    assert facts.snapshot() == before


@pytest.mark.integration
def test_audit_function_rejects_person_version_that_contradicts_fixed_summary(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """目标与单元即使同为 v2，也不能生成固定宣称 v1 的人员导入审计。"""

    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-audit-person-v2-{uuid4().hex}",
    )
    idempotency_key_hash = sha256(context.idempotency_key.encode("utf-8")).hexdigest()
    person_id = _seed_confirmed_person_name(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
        display_name="版本矛盾教师",
        external_reference=f"audit-person-v2-{uuid4().hex}",
    )
    record_id = uuid4()
    batch_id = uuid4()
    unit_id = uuid4()
    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET LOCAL ROLE campus_ai_runtime")
        set_local_execution_context(cast(Any, connection), _envelope_factory(context))
        connection.execute("RESET ROLE")
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        connection.execute(
            "UPDATE school_people SET version = 2 WHERE school_id = %s AND id = %s",
            (import_actor_facts.school_id, person_id),
        )
        connection.execute(
            """
            INSERT INTO command_idempotency_records (
                id, school_id, action_code, actor_user_id, actor_person_id,
                actor_context_type, actor_context_key, actor_assignment_id,
                actor_assignment_version, resource_type, resource_id,
                idempotency_key_hash, request_sha256, status, result_type, result_payload
            ) VALUES (
                %s, %s, 'master_data.import.manual_confirm', %s, %s,
                'assignment', %s, %s, 1, 'master_data.import_batch', %s,
                %s, %s, 'succeeded', 'master_data.import_outcome',
                jsonb_build_object(
                    'resource_type', 'master_data.import_batch',
                    'resource_id', %s::text,
                    'resource_version', 1,
                    'outcome_code', 'applied'
                )
            )
            """,
            (
                record_id,
                import_actor_facts.school_id,
                import_actor_facts.actor_user_id,
                import_actor_facts.actor_person_id,
                f"assignment:{import_actor_facts.actor_assignment_id}:1",
                import_actor_facts.actor_assignment_id,
                str(batch_id),
                idempotency_key_hash,
                "b" * 64,
                str(batch_id),
            ),
        )
        connection.execute(
            """
            INSERT INTO master_data_import_batches (
                id, school_id, mode, source_kind, source_ref, source_sha256,
                worksheet_names, status, planned_units, succeeded_units,
                failed_units, pending_units, confirmation_card_sha256,
                confirmed_by_user_id, confirmed_by_person_id,
                confirmed_by_assignment_id, confirmed_at, request_id,
                idempotency_key_hash, version
            ) VALUES (
                %s, %s, 'manual_unit', 'manual', %s, %s,
                '[]'::jsonb, 'applied', 1, 1, 0, 0, %s,
                %s, %s, %s, clock_timestamp(), %s, %s, 1
            )
            """,
            (
                batch_id,
                import_actor_facts.school_id,
                f"manual-audit-v2-{uuid4().hex}",
                "c" * 64,
                "d" * 64,
                import_actor_facts.actor_user_id,
                import_actor_facts.actor_person_id,
                import_actor_facts.actor_assignment_id,
                context.request_id,
                idempotency_key_hash,
            ),
        )
        connection.execute(
            """
            INSERT INTO master_data_import_units (
                id, school_id, batch_id, sequence, row_number, unit_kind,
                status, payload, payload_sha256, soft_anomalies,
                initial_soft_anomalies, target_id, target_version, version
            ) VALUES (
                %s, %s, %s, 1, 1, 'person', 'applied', '{}'::jsonb, %s,
                '[]'::jsonb, '[]'::jsonb, %s, 2, 1
            )
            """,
            (unit_id, import_actor_facts.school_id, batch_id, "e" * 64, person_id),
        )
        connection.execute(
            """
            INSERT INTO master_data_import_command_outcomes (
                id, school_id, idempotency_record_id, batch_id, action_code,
                status, batch_version, succeeded_units, failed_units, executed_units
            ) VALUES (
                %s, %s, %s, %s, 'master_data.import.manual_confirm',
                'applied', 1, ARRAY[1], ARRAY[]::integer[], ARRAY[1]
            )
            """,
            (uuid4(), import_actor_facts.school_id, record_id, batch_id),
        )
        connection.commit()

    _, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    facts = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=context,
        school_id=import_actor_facts.school_id,
        batch_id=batch_id,
    )
    before = facts.snapshot()
    with pytest.raises(DBAPIError, match="master data import audit fact mismatch"):
        with uow_factory.open(context) as uow:
            uow.session.execute(
                text(
                    """
                    SELECT app.append_master_data_import_audit(
                        :actor_user_id, :school_id, :actor_person_id,
                        :actor_assignment_id, 1, :idempotency_key_hash,
                        'master_data.person.imported', 'school_person',
                        :target_id, :batch_id, :unit_id,
                        jsonb_build_object(
                            'state_code', 'person.absent',
                            'resource_version', NULL,
                            'stable_reference_ids', '[]'::jsonb,
                            'fact_codes', '[]'::jsonb
                        ),
                        jsonb_build_object(
                            'state_code', 'person.confirmed',
                            'resource_version', 1,
                            'stable_reference_ids', jsonb_build_array(
                                CAST(:target_id AS text)
                            ),
                            'fact_codes', '["person.imported"]'::jsonb
                        ),
                        :request_id, 'manual.person-import-confirmed'
                    )
                    """
                ),
                {
                    "actor_user_id": import_actor_facts.actor_user_id,
                    "school_id": import_actor_facts.school_id,
                    "actor_person_id": import_actor_facts.actor_person_id,
                    "actor_assignment_id": import_actor_facts.actor_assignment_id,
                    "idempotency_key_hash": idempotency_key_hash,
                    "target_id": person_id,
                    "batch_id": batch_id,
                    "unit_id": unit_id,
                    "request_id": context.request_id,
                },
            ).scalar_one()
    assert facts.snapshot() == before


@pytest.mark.integration
def test_audit_function_rejects_takeover_facts_committed_before_audit(
    current_master_data_schema: PostgresRoles,
    import_actor_facts: ImportActorFacts,
) -> None:
    """接管组成事实不得先于固定审计跨事务提交，失败后完整写集合不变。"""

    new_actor = _seed_same_school_import_actor(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
    )
    context = _command_context(
        new_actor,
        idempotency_key=f"task8-audit-takeover-{uuid4().hex}",
    )
    original_context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-audit-takeover-source-{uuid4().hex}",
    )
    batch_id = uuid4()
    previous_record_id = uuid4()
    new_record_id = uuid4()

    def snapshot_takeover_writes() -> tuple[Any, ...]:
        with current_master_data_schema.migration_connection() as snapshot_connection:
            snapshot_connection.execute("SET ROLE campus_ai_db_owner")
            row = snapshot_connection.execute(
                """
                SELECT
                    (SELECT jsonb_agg(to_jsonb(item) ORDER BY item.id)
                     FROM command_idempotency_records AS item WHERE school_id = %s),
                    (SELECT jsonb_agg(to_jsonb(item) ORDER BY item.id)
                     FROM master_data_import_batches AS item WHERE school_id = %s),
                    (SELECT jsonb_agg(to_jsonb(item) ORDER BY item.id)
                     FROM master_data_import_units AS item WHERE school_id = %s),
                    (SELECT jsonb_agg(to_jsonb(item) ORDER BY item.id)
                     FROM master_data_import_takeovers AS item WHERE school_id = %s),
                    (SELECT jsonb_agg(to_jsonb(item) ORDER BY item.id)
                     FROM audit_logs AS item WHERE school_id = %s)
                """,
                (import_actor_facts.school_id,) * 5,
            ).fetchone()
        assert row is not None
        return tuple(row)

    before_partial_commit = snapshot_takeover_writes()
    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET LOCAL ROLE campus_ai_runtime")
        set_local_execution_context(
            cast(Any, connection),
            _envelope_factory(original_context),
        )
        connection.execute("RESET ROLE")
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO command_idempotency_records (
                id, school_id, action_code, actor_user_id, actor_person_id,
                actor_context_type, actor_context_key, actor_assignment_id,
                actor_assignment_version, resource_type, resource_id,
                idempotency_key_hash, request_sha256, status, result_type, result_payload
            ) VALUES (
                %s, %s, 'master_data.import.ordinary_resume', %s, %s,
                'assignment', %s, %s, 1, 'master_data.import_batch', %s,
                %s, %s, 'terminal_failure', 'master_data.import_outcome',
                jsonb_build_object(
                    'resource_type', 'master_data.import_batch',
                    'resource_id', %s::text,
                    'resource_version', 1,
                    'outcome_code', 'master_data.import.command_superseded'
                )
            ), (
                %s, %s, 'master_data.import.ordinary_takeover', %s, %s,
                'assignment', %s, %s, 1, 'master_data.import_batch', %s,
                %s, %s, 'in_progress', NULL, NULL
            )
            """,
            (
                previous_record_id,
                import_actor_facts.school_id,
                import_actor_facts.actor_user_id,
                import_actor_facts.actor_person_id,
                f"assignment:{import_actor_facts.actor_assignment_id}:1",
                import_actor_facts.actor_assignment_id,
                str(batch_id),
                "6" * 64,
                "7" * 64,
                str(batch_id),
                new_record_id,
                new_actor.school_id,
                new_actor.actor_user_id,
                new_actor.actor_person_id,
                f"assignment:{new_actor.actor_assignment_id}:1",
                new_actor.actor_assignment_id,
                str(batch_id),
                sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
                "8" * 64,
            ),
        )
        connection.execute(
            """
            INSERT INTO master_data_import_batches (
                id, school_id, mode, source_kind, source_ref, source_sha256,
                filename_summary, source_size_bytes, worksheet_names, status,
                planned_units, succeeded_units, failed_units, pending_units,
                confirmed_by_user_id, confirmed_by_person_id,
                confirmed_by_assignment_id, confirmed_at, request_id,
                idempotency_key_hash, active_idempotency_record_id,
                active_action_code, version
            ) VALUES (
                %s, %s, 'ordinary_atomic_units', 'excel', %s, %s,
                'takeover-audit.xlsx', 1024, '[]'::jsonb, 'applying',
                1, 0, 0, 1, %s, %s, %s, clock_timestamp(), %s,
                %s, %s, 'master_data.import.ordinary_takeover', 2
            )
            """,
            (
                batch_id,
                import_actor_facts.school_id,
                f"protected-{uuid4().hex}",
                "9" * 64,
                import_actor_facts.actor_user_id,
                import_actor_facts.actor_person_id,
                import_actor_facts.actor_assignment_id,
                uuid4(),
                "a" * 64,
                new_record_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO master_data_import_units (
                id, school_id, batch_id, sequence, row_number, unit_kind, status,
                payload, payload_sha256, soft_anomalies, initial_soft_anomalies,
                last_attempt_idempotency_record_id, last_attempt_completed, version
            ) VALUES (
                %s, %s, %s, 1, 1, 'person', 'ready',
                '{}'::jsonb, %s, '[]'::jsonb, '[]'::jsonb, %s, false, 1
            )
            """,
            (
                uuid4(),
                import_actor_facts.school_id,
                batch_id,
                "b" * 64,
                previous_record_id,
            ),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.commit()
        assert snapshot_takeover_writes() == before_partial_commit


@pytest.mark.integration
def _retired_legacy_test_audit_function_accepts_takeover_facts_created_in_same_transaction(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """接管事实与固定审计位于同一事务时只允许追加一条安全记录。"""

    new_actor = _seed_same_school_import_actor(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
    )
    context = _command_context(
        new_actor,
        idempotency_key=f"task8-audit-same-xact-takeover-{uuid4().hex}",
    )
    batch_id = uuid4()
    takeover_id = uuid4()
    previous_record_id = uuid4()
    confirmer_before = _seed_takeover_prestate(
        current_master_data_schema,
        original_actor=import_actor_facts,
        batch_id=batch_id,
        previous_record_id=previous_record_id,
    )
    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        _insert_takeover_audit_fact_set(
            connection,
            context=context,
            original_actor=import_actor_facts,
            new_actor=new_actor,
            batch_id=batch_id,
            previous_record_id=previous_record_id,
            new_record_id=uuid4(),
            takeover_id=takeover_id,
        )
        _append_takeover_audit(
            connection,
            context=context,
            actor=new_actor,
            batch_id=batch_id,
            takeover_id=takeover_id,
        )
        connection.commit()

    _, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    facts = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=context,
        school_id=import_actor_facts.school_id,
        batch_id=batch_id,
    )
    snapshot = facts.snapshot()
    audit_rows = snapshot[-2]
    matching_rows = [
        row
        for row in audit_rows
        if row["event_type"] == "master_data.import.taken_over"
        and row["target_id"] == str(takeover_id)
    ]
    assert len(matching_rows) == 1
    assert "仅保存在接管事实中的自由文本" not in str(matching_rows[0])
    assert matching_rows[0]["reason"] == "manual.ordinary-import-taken-over"
    batch_row = snapshot[0]
    assert batch_row is not None
    assert (
        batch_row["confirmed_by_user_id"],
        UUID(batch_row["confirmed_by_person_id"]),
        UUID(batch_row["confirmed_by_assignment_id"]),
        datetime.fromisoformat(batch_row["confirmed_at"]),
    ) == confirmer_before


@pytest.mark.integration
def _retired_legacy_test_person_audit_accepts_takeover_chain_after_batch_version_advances(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """A takeover remains the active chain after a later unit increments batch.version."""

    new_actor = _seed_same_school_import_actor(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
    )
    context = _command_context(
        new_actor,
        idempotency_key=f"task8-audit-takeover-unit-{uuid4().hex}",
    )
    batch_id = uuid4()
    previous_record_id = uuid4()
    new_record_id = uuid4()
    takeover_id = uuid4()
    unit_id = uuid4()
    person_id = uuid4()
    _seed_takeover_prestate(
        current_master_data_schema,
        original_actor=import_actor_facts,
        batch_id=batch_id,
        previous_record_id=previous_record_id,
        unit_id=unit_id,
    )
    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        _insert_takeover_audit_fact_set(
            connection,
            context=context,
            original_actor=import_actor_facts,
            new_actor=new_actor,
            batch_id=batch_id,
            previous_record_id=previous_record_id,
            new_record_id=new_record_id,
            takeover_id=takeover_id,
        )
        _append_takeover_audit(
            connection,
            context=context,
            actor=new_actor,
            batch_id=batch_id,
            takeover_id=takeover_id,
        )
        connection.commit()
        connection.execute("SET LOCAL ROLE campus_ai_runtime")
        set_local_execution_context(cast(Any, connection), _envelope_factory(context))
        connection.execute("RESET ROLE")
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        connection.execute(
            "INSERT INTO school_people (id, school_id, status, version) "
            "VALUES (%s, %s, 'active', 1)",
            (person_id, new_actor.school_id),
        )
        connection.execute(
            """
            UPDATE master_data_import_units
            SET status = 'applied',
                target_id = %s,
                target_version = 1,
                last_attempt_idempotency_record_id = %s,
                last_attempt_completed = true,
                version = 2
            WHERE school_id = %s AND batch_id = %s AND id = %s
            """,
            (
                person_id,
                new_record_id,
                new_actor.school_id,
                batch_id,
                unit_id,
            ),
        )
        connection.execute(
            """
            UPDATE master_data_import_batches
            SET succeeded_units = 1, pending_units = 0, version = 3
            WHERE school_id = %s AND id = %s
            """,
            (new_actor.school_id, batch_id),
        )
        _append_manual_audit(
            connection,
            context=context,
            actor=new_actor,
            person_id=person_id,
            batch_id=batch_id,
            unit_id=unit_id,
            claim_same_name_continuation=False,
            reason_code="excel.person-import-confirmed",
        )
        connection.commit()

    _, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    snapshot = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=context,
        school_id=new_actor.school_id,
        batch_id=batch_id,
    ).snapshot()
    assert (
        sum(
            row["event_type"] == "master_data.person.imported"
            and row["target_id"] == str(person_id)
            for row in snapshot[-2]
        )
        == 1
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "violation",
    (
        "wrong_previous_action",
        "wrong_previous_resource",
        "wrong_previous_result_batch",
        "wrong_original_actor",
        "wrong_original_version",
        "wrong_new_action",
        "wrong_new_resource",
        "wrong_new_actor",
        "wrong_new_version",
        "wrong_request",
        "wrong_batch_versions",
    ),
)
def test_audit_function_rejects_false_takeover_semantics_in_same_transaction(
    violation: str,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """接管链任一稳定事实不符时，当前事务全部事实与审计必须一起回滚。"""

    new_actor = _seed_same_school_import_actor(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
    )
    context = _command_context(
        new_actor,
        idempotency_key=f"task8-audit-takeover-{violation}-{uuid4().hex}",
    )
    batch_id = uuid4()
    takeover_id = uuid4()
    previous_record_id = uuid4()
    _seed_takeover_prestate(
        current_master_data_schema,
        original_actor=import_actor_facts,
        batch_id=batch_id,
        previous_record_id=previous_record_id,
        previous_action=(
            "master_data.import.manual_confirm"
            if violation == "wrong_previous_action"
            else "master_data.import.ordinary_resume"
        ),
        previous_resource_type=(
            "school_person"
            if violation == "wrong_previous_resource"
            else "master_data.import_batch"
        ),
        previous_resource_id=(uuid4() if violation == "wrong_previous_resource" else batch_id),
    )
    _, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    facts = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=context,
        school_id=import_actor_facts.school_id,
        batch_id=batch_id,
    )
    before = facts.snapshot()
    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        try:
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="master data import audit fact mismatch",
            ):
                _insert_takeover_audit_fact_set(
                    connection,
                    context=context,
                    original_actor=import_actor_facts,
                    new_actor=new_actor,
                    batch_id=batch_id,
                    previous_record_id=previous_record_id,
                    new_record_id=uuid4(),
                    takeover_id=takeover_id,
                    violation=violation,
                )
                _append_takeover_audit(
                    connection,
                    context=context,
                    actor=new_actor,
                    batch_id=batch_id,
                    takeover_id=takeover_id,
                )
        finally:
            connection.rollback()
    assert facts.snapshot() == before


@pytest.mark.integration
def _retired_legacy_test_manual_unit_uses_same_preview_and_confirmation_gate(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """手工录入也必须先预览；正式人员、审计和 Outbox 只能由人工确认同事务产生。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    uow_factory = application.SqlAlchemyMasterDataImportUoWFactory(
        session_factory=import_runtime_session_factory,
        envelope_factory=_envelope_factory,
        lock_timeout_ms=750,
    )
    service = application.MasterDataImportService(
        uow_factory=uow_factory,
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    external_reference = f"T-{uuid4().hex}"
    preview = service.stage_manual(
        contracts.StageManualPersonUnitCommand(
            source_reference=f"manual-card-{uuid4().hex}",
            unit=contracts.PersonImportUnitDraft(
                row_number=1,
                display_name="测试教师",
                external_reference=external_reference,
                valid_from=datetime.now(UTC).replace(microsecond=0),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-manual-{uuid4().hex}",
        ),
    )

    assert preview.status == "ready_for_confirmation"
    assert preview.formal_writes == 0
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        before = connection.execute(
            """
            SELECT count(*) FROM school_person_names
            WHERE school_id = %s AND source_kind = 'person_external_reference'
              AND source_ref = %s
            """,
            (import_actor_facts.school_id, external_reference),
        ).fetchone()
    assert before == (0,)

    confirmation_context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-confirm-manual-{uuid4().hex}",
    )
    outcome = service.confirm(
        contracts.ConfirmImportBatchCommand(
            batch_id=preview.batch_id,
            expected_batch_version=preview.batch_version,
            confirmation_card_sha256=preview.confirmation_card_sha256,
            decisions=(),
        ),
        context=confirmation_context,
    )

    assert outcome.status == "applied"
    assert outcome.succeeded_units == (1,)
    assert outcome.failed_units == ()
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        formal = connection.execute(
            """
            SELECT person.status, name.display_name, name.source_ref, name.status
            FROM school_people AS person
            JOIN school_person_names AS name
              ON name.school_id = person.school_id AND name.person_id = person.id
            WHERE person.school_id = %s
              AND name.source_kind = 'person_external_reference'
              AND name.source_ref = %s
            """,
            (import_actor_facts.school_id, external_reference),
        ).fetchall()
        audit_count = connection.execute(
            """
            SELECT count(*) FROM audit_logs
            WHERE school_id = %s AND request_id = %s
              AND event_type = 'master_data.person.imported'
            """,
            (import_actor_facts.school_id, str(confirmation_context.request_id)),
        ).fetchone()
        outbox_count = connection.execute(
            """
            SELECT count(*) FROM outbox_messages
            WHERE school_id = %s AND event_type = 'master_data.person.imported'
            """,
            (import_actor_facts.school_id,),
        ).fetchone()
    assert formal == [("active", "测试教师", external_reference, "confirmed")]
    assert audit_count == (1,)
    assert outbox_count == (1,)


@pytest.mark.integration
def _retired_legacy_test_invalid_original_assignment_can_be_explicitly_taken_over(
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    scenario = applying_import_scenario
    scenario.revoke_original_assignment()
    actor = scenario.replacement_context.actor
    assert type(actor) is StaffActorContext
    identity = scenario.replacement_service._takeover_idempotency_identity(  # pyright: ignore[reportPrivateUsage]
        scenario.takeover_command,
        scenario.replacement_context,
        actor,
    )
    checkpoint = scenario.replacement_service._execute_takeover_transaction(  # pyright: ignore[reportPrivateUsage]
        scenario.takeover_command,
        context=scenario.replacement_context,
        actor=actor,
        identity=identity,
    )

    assert checkpoint.status == "takeover_committed"
    assert checkpoint.batch_id == scenario.batch_id
    assert checkpoint.batch_version_after == scenario.takeover_command.expected_batch_version + 1
    assert checkpoint.transferred_sequences == (1,)
    assert scenario.read_original_confirmer() == scenario.original_confirmer
    assert scenario.read_takeover_count() == 1
    with scenario.roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        row = connection.execute(
            """
            SELECT old_record.status,
                   old_record.result_payload ->> 'outcome_code',
                   (old_record.result_payload ->> 'resource_version')::bigint,
                   batch.status, batch.active_idempotency_record_id,
                   batch.active_action_code, batch.version,
                   unit.last_attempt_idempotency_record_id,
                   unit.last_attempt_completed,
                   takeover.id, takeover.new_idempotency_record_id,
                   takeover.takeover_reason,
                   takeover.transferred_sequences,
                   takeover.transferred_unit_versions,
                   (SELECT count(*) FROM audit_logs
                    WHERE school_id = batch.school_id
                      AND event_type = 'master_data.import.taken_over'),
                   (SELECT count(*) FROM outbox_messages
                    WHERE school_id = batch.school_id)
            FROM master_data_import_batches AS batch
            JOIN master_data_import_units AS unit
              ON unit.school_id = batch.school_id AND unit.batch_id = batch.id
            JOIN command_idempotency_records AS old_record
              ON old_record.school_id = batch.school_id
             AND old_record.id = %s
            JOIN master_data_import_takeovers AS takeover
              ON takeover.school_id = batch.school_id
             AND takeover.batch_id = batch.id
            WHERE batch.school_id = %s AND batch.id = %s AND unit.sequence = 1
            """,
            (
                scenario.old_idempotency_record_id,
                scenario.school_id,
                scenario.batch_id,
            ),
        ).fetchone()
    assert row is not None
    assert row[:11] == (
        "terminal_failure",
        "master_data.import.command_superseded",
        scenario.takeover_command.expected_batch_version,
        "applying",
        checkpoint.new_idempotency_record_id,
        "master_data.import.ordinary_takeover",
        checkpoint.batch_version_after,
        checkpoint.new_idempotency_record_id,
        False,
        checkpoint.takeover_id,
        checkpoint.new_idempotency_record_id,
    )
    assert row[11:] == (
        scenario.takeover_command.takeover_reason,
        [1],
        [scenario.takeover_command.units[0].expected_unit_version],
        1,
        0,
    )
    assert set(checkpoint.__dataclass_fields__) == {
        "status",
        "batch_id",
        "takeover_id",
        "batch_version_after",
        "new_idempotency_record_id",
        "transferred_sequences",
    }


@pytest.mark.integration
def _retired_legacy_test_runtime_cannot_commit_lone_supersede_while_original_assignment_is_valid(
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """固定 CAS 缺少完整接管 bundle 或原任职仍有效时不得单独制造 zombie。"""

    idempotency_contracts = importlib.import_module("app.platform.idempotency.contracts")
    scenario = applying_import_scenario
    before = scenario.snapshot_all_schools()
    actor = scenario.replacement_context.actor
    assert type(actor) is StaffActorContext
    with pytest.raises((idempotency_contracts.IdempotencyPayloadConflict, DBAPIError)):
        with scenario.replacement_service._uow_factory.open(  # pyright: ignore[reportPrivateUsage]
            scenario.replacement_context
        ) as uow:
            old_record = uow.imports.lock_takeover_source(
                actor.school_id,
                scenario.batch_id,
                scenario.old_idempotency_record_id,
            )
            assert old_record is not None
            uow.imports.supersede_takeover_source(
                actor.school_id,
                scenario.batch_id,
                old_record.record_id,
                expected_request_sha256=old_record.request_sha256,
                batch_version_before=scenario.takeover_command.expected_batch_version,
            )
            uow.commit()
    assert scenario.snapshot_all_schools() == before


@pytest.mark.integration
@pytest.mark.parametrize("missing_component", ["new_record", "takeover", "batch", "units"])
def _retired_legacy_test_runtime_final_cas_rejects_each_missing_takeover_bundle_component(
    missing_component: str,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """原任职失效仍不足以授权 CAS；同事务 bundle 任一组成缺失必须整体回滚。"""

    idempotency_contracts = importlib.import_module("app.platform.idempotency.contracts")
    scenario = applying_import_scenario
    scenario.invalidate_original_assignment("status")
    before = scenario.snapshot_all_schools()
    with pytest.raises((idempotency_contracts.IdempotencyPayloadConflict, DBAPIError)):
        _execute_direct_takeover_bundle(
            scenario,
            missing_component=missing_component,
        )
    assert scenario.snapshot_all_schools() == before


@pytest.mark.integration
def _retired_legacy_test_runtime_final_cas_accepts_only_complete_current_transaction_bundle_and_audits_once(  # noqa: E501
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """完整 current-xact bundle 才能终止旧命令，并由固定边界恰写一条审计。"""

    scenario = applying_import_scenario
    scenario.invalidate_original_assignment("status")
    takeover_id, new_record_id = _execute_direct_takeover_bundle(scenario)
    with scenario.roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        row = connection.execute(
            """
            SELECT old_record.status,
                   old_record.result_payload ->> 'outcome_code',
                   batch.active_idempotency_record_id,
                   unit.last_attempt_idempotency_record_id,
                   takeover.id,
                   (SELECT count(*) FROM audit_logs
                    WHERE school_id = batch.school_id
                      AND event_type = 'master_data.import.taken_over'
                      AND target_id = takeover.id::text)
            FROM master_data_import_batches AS batch
            JOIN master_data_import_units AS unit
              ON unit.school_id = batch.school_id AND unit.batch_id = batch.id
            JOIN master_data_import_takeovers AS takeover
              ON takeover.school_id = batch.school_id AND takeover.batch_id = batch.id
            JOIN command_idempotency_records AS old_record
              ON old_record.school_id = batch.school_id AND old_record.id = %s
            WHERE batch.school_id = %s AND batch.id = %s
            """,
            (
                scenario.old_idempotency_record_id,
                scenario.school_id,
                scenario.batch_id,
            ),
        ).fetchone()
    assert row == (
        "terminal_failure",
        "master_data.import.command_superseded",
        new_record_id,
        new_record_id,
        takeover_id,
        1,
    )


@pytest.mark.integration
def _retired_legacy_test_takeover_final_cas_accepts_complete_bundle_released_from_savepoint(
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """合法接管 bundle 经 SAVEPOINT 释放后仍由固定 CAS+audit 与延后约束整体提交。"""

    scenario = applying_import_scenario
    scenario.invalidate_original_assignment("status")
    takeover_id, new_record_id = _execute_direct_takeover_bundle(
        scenario,
        bundle_in_savepoint=True,
    )
    with scenario.roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        durable_facts = connection.execute(
            """
            SELECT old_record.status,
                   old_record.result_payload ->> 'outcome_code',
                   batch.active_idempotency_record_id,
                   unit.last_attempt_idempotency_record_id,
                   takeover.id,
                   (SELECT count(*) FROM audit_logs
                    WHERE school_id = batch.school_id
                      AND event_type = 'master_data.import.taken_over'
                      AND target_id = takeover.id::text)
            FROM master_data_import_batches AS batch
            JOIN master_data_import_units AS unit
              ON unit.school_id = batch.school_id AND unit.batch_id = batch.id
            JOIN master_data_import_takeovers AS takeover
              ON takeover.school_id = batch.school_id AND takeover.batch_id = batch.id
            JOIN command_idempotency_records AS old_record
              ON old_record.school_id = batch.school_id AND old_record.id = %s
            WHERE batch.school_id = %s AND batch.id = %s
            """,
            (
                scenario.old_idempotency_record_id,
                scenario.school_id,
                scenario.batch_id,
            ),
        ).fetchone()
    assert durable_facts == (
        "terminal_failure",
        "master_data.import.command_superseded",
        new_record_id,
        new_record_id,
        takeover_id,
        1,
    )


@pytest.mark.integration
def _retired_legacy_test_runtime_cannot_commit_takeover_bundle_without_final_boundary(
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """接管组成事实缺少固定 CAS 与审计时，延后完整性约束必须整体拒绝提交。"""

    scenario = applying_import_scenario
    scenario.invalidate_original_assignment("status")
    before = scenario.snapshot_all_schools()
    with pytest.raises(DBAPIError):
        _execute_direct_takeover_bundle(
            scenario,
            call_final_boundary=False,
        )
    assert scenario.snapshot_all_schools() == before


@pytest.mark.integration
def _retired_legacy_test_runtime_final_cas_rejects_wrong_request_bundle_with_full_rollback(
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """接管事实不是当前 request 时，即使其余 bundle 完整也不能成为 CAS 权威。"""

    idempotency_contracts = importlib.import_module("app.platform.idempotency.contracts")
    scenario = applying_import_scenario
    scenario.invalidate_original_assignment("status")
    before = scenario.snapshot_all_schools()
    with pytest.raises((idempotency_contracts.IdempotencyPayloadConflict, DBAPIError)):
        _execute_direct_takeover_bundle(scenario, takeover_request_id=uuid4())
    assert scenario.snapshot_all_schools() == before


@pytest.mark.integration
@pytest.mark.parametrize(
    "unit_corruption",
    [
        "transferred_applied",
        "transferred_version_changed",
        "transferred_completed",
        "extra_incomplete",
    ],
)
def _retired_legacy_test_runtime_final_cas_rejects_non_exact_current_transaction_unit_bundle(
    unit_corruption: str,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """当前事务中的非法完成状态或额外单元不得伪装成精确接管 bundle。"""

    scenario = applying_import_scenario
    scenario.invalidate_original_assignment("status")
    before = scenario.snapshot_all_schools()
    with pytest.raises(DBAPIError):
        _execute_direct_takeover_bundle(
            scenario,
            unit_corruption=unit_corruption,
        )
    assert scenario.snapshot_all_schools() == before


@pytest.mark.integration
def _retired_legacy_test_takeover_manifest_rejects_completed_failed_unit_reopened_before_capture(
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """已完成失败单元即使被顶层或 SAVEPOINT 改回未完成，也不能进入转移清单。"""

    scenario = applying_import_scenario
    original_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    original_service._record_unit_transaction_failure(  # pyright: ignore[reportPrivateUsage]
        batch_id=scenario.batch_id,
        sequence=1,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-takeover-record-failed-{uuid4().hex}",
        ),
        eligible_statuses=("ready",),
        idempotency_record_id=scenario.old_idempotency_record_id,
    )
    scenario.invalidate_original_assignment("status")
    for unit_corruption in (
        "pre_manifest_completed_failed_to_incomplete",
        "pre_manifest_subtransaction_completed_failed_to_incomplete",
    ):
        before = scenario.snapshot_all_schools()
        with pytest.raises(DBAPIError):
            _execute_direct_takeover_bundle(
                scenario,
                unit_corruption=unit_corruption,
            )
        assert scenario.snapshot_all_schools() == before


@pytest.mark.integration
def _retired_legacy_test_committed_completed_attempt_cannot_be_reintroduced_as_takeover_manifest_source(  # noqa: E501
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """已提交的 completed attempt 不得由另一事务改回旧命令 incomplete 后进入接管清单。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    scenario = applying_import_scenario
    original_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    original_service._record_unit_transaction_failure(  # pyright: ignore[reportPrivateUsage]
        batch_id=scenario.batch_id,
        sequence=1,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-takeover-committed-failure-{uuid4().hex}",
        ),
        eligible_statuses=("ready",),
        idempotency_record_id=scenario.old_idempotency_record_id,
    )
    before_transaction_a = scenario.snapshot_all_schools()

    actor = scenario.replacement_context.actor
    assert type(actor) is StaffActorContext
    try:
        with scenario.replacement_service._uow_factory.open(  # pyright: ignore[reportPrivateUsage]
            scenario.replacement_context
        ) as uow:
            now = uow.current_database_time()
            scenario.replacement_service._authorize(  # pyright: ignore[reportPrivateUsage]
                uow,
                actor,
                now,
            )
            unit = uow.imports.lock_unit(actor.school_id, scenario.batch_id, 1)
            assert unit is not None
            assert (
                unit.status,
                unit.last_attempt_idempotency_record_id,
                unit.last_attempt_completed,
            ) == ("failed", scenario.old_idempotency_record_id, True)
            unit.last_attempt_completed = False
            uow.imports.save_unit(unit)
            uow.commit()
    except DBAPIError:
        assert scenario.snapshot_all_schools() == before_transaction_a
        return

    after_transaction_a = scenario.snapshot_all_schools()
    assert after_transaction_a != before_transaction_a
    snapshot_table_names = (
        "batches",
        "units",
        "people",
        "person_names",
        "idempotency_records",
        "command_outcomes",
        "takeovers",
        "audit_logs",
        "outbox_messages",
    )
    assert {
        table_name
        for table_name, before, after in zip(
            snapshot_table_names,
            before_transaction_a,
            after_transaction_a,
            strict=True,
        )
        if before != after
    } == {"units"}
    preview = scenario.replacement_service.preview(
        scenario.batch_id,
        context=scenario.replacement_context,
    )
    scenario.invalidate_original_assignment("status")
    command = contracts.TakeOverOrdinaryImportCommand(
        batch_id=scenario.batch_id,
        expected_batch_version=preview.batch_version,
        confirmation_card_sha256=preview.confirmation_card_sha256,
        units=tuple(
            contracts.TakeoverImportUnitDecision(
                sequence=unit.sequence,
                expected_unit_version=unit.unit_version,
                continue_reason=("接管人复核同名人员后继续" if unit.soft_anomaly_codes else None),
            )
            for unit in preview.units
        ),
        takeover_reason="原任职失效后接管仍未完成的单元",
    )
    before_transaction_b = scenario.snapshot_all_schools()
    checkpoint = scenario.replacement_service.take_over(
        command,
        context=scenario.replacement_context,
    )
    after_transaction_b = scenario.snapshot_all_schools()
    assert after_transaction_b != before_transaction_b
    assert {
        table_name
        for table_name, before, after in zip(
            snapshot_table_names,
            before_transaction_b,
            after_transaction_b,
            strict=True,
        )
        if before != after
    } == {"batches", "units", "idempotency_records", "takeovers", "audit_logs"}
    with scenario.roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        committed_bundle = connection.execute(
            """
            SELECT takeover.transferred_sequences,
                   takeover.transferred_unit_versions,
                   old_record.status,
                   batch.active_idempotency_record_id,
                   unit.last_attempt_idempotency_record_id,
                   unit.last_attempt_completed,
                   (SELECT count(*) FROM audit_logs AS audit
                    WHERE audit.school_id = takeover.school_id
                      AND audit.event_type = 'master_data.import.taken_over')
            FROM master_data_import_takeovers AS takeover
            JOIN command_idempotency_records AS old_record
              ON old_record.school_id = takeover.school_id
             AND old_record.id = takeover.previous_idempotency_record_id
            JOIN master_data_import_batches AS batch
              ON batch.school_id = takeover.school_id
             AND batch.id = takeover.batch_id
            JOIN master_data_import_units AS unit
              ON unit.school_id = takeover.school_id
             AND unit.batch_id = takeover.batch_id
             AND unit.sequence = 1
            WHERE takeover.school_id = %s AND takeover.id = %s
            """,
            (scenario.school_id, checkpoint.takeover_id),
        ).fetchone()
    assert committed_bundle is not None
    assert tuple(committed_bundle[0]) == (1,)
    assert tuple(committed_bundle[1]) == (3,)
    assert committed_bundle[2] == "terminal_failure"
    assert committed_bundle[3] == checkpoint.new_idempotency_record_id
    assert committed_bundle[4] == checkpoint.new_idempotency_record_id
    assert committed_bundle[5] is False
    assert committed_bundle[6] == 1
    pytest.fail("a committed completed attempt was accepted as takeover manifest prestate")


@pytest.mark.integration
def _retired_legacy_test_runtime_final_cas_rejects_cross_school_target_with_all_school_rollback(
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """直调固定边界也不得跨校引用批次，且所有学校事实都不变。"""

    scenario = applying_import_scenario
    scenario.invalidate_original_assignment("status")
    actor = scenario.replacement_context.actor
    assert type(actor) is StaffActorContext
    before = scenario.snapshot_all_schools()
    with pytest.raises(DBAPIError):
        with scenario.replacement_service._uow_factory.open(  # pyright: ignore[reportPrivateUsage]
            scenario.replacement_context
        ) as uow:
            uow.imports.supersede_takeover_source(
                actor.school_id,
                scenario.cross_school_batch_id,
                scenario.old_idempotency_record_id,
                expected_request_sha256="0" * 64,
                batch_version_before=scenario.takeover_command.expected_batch_version,
            )
            uow.commit()
    assert scenario.snapshot_all_schools() == before


@pytest.mark.integration
def _retired_legacy_test_runtime_final_cas_rejects_history_record_after_takeover_is_current(
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """旧命令转为历史后，固定边界重入不得再次 CAS 或追加审计。"""

    scenario = applying_import_scenario
    with scenario.roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        old_request_sha256 = connection.execute(
            """
            SELECT request_sha256
            FROM command_idempotency_records
            WHERE school_id = %s AND id = %s
            """,
            (scenario.school_id, scenario.old_idempotency_record_id),
        ).fetchone()
    assert old_request_sha256 is not None
    scenario.invalidate_original_assignment("status")
    scenario.replacement_service.take_over(
        scenario.takeover_command,
        context=scenario.replacement_context,
    )
    before = scenario.snapshot_all_schools()
    actor = scenario.replacement_context.actor
    assert type(actor) is StaffActorContext
    with pytest.raises(DBAPIError):
        with scenario.replacement_service._uow_factory.open(  # pyright: ignore[reportPrivateUsage]
            scenario.replacement_context
        ) as uow:
            uow.imports.supersede_takeover_source(
                actor.school_id,
                scenario.batch_id,
                scenario.old_idempotency_record_id,
                expected_request_sha256=str(old_request_sha256[0]),
                batch_version_before=scenario.takeover_command.expected_batch_version,
            )
            uow.commit()
    assert scenario.snapshot_all_schools() == before


@pytest.mark.integration
@pytest.mark.parametrize(
    "invalidity",
    ["person", "version", "status", "valid_from", "valid_to"],
)
def _retired_legacy_test_each_original_assignment_invalidity_independently_allows_takeover(
    invalidity: str,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """person/version/status/有效期任一单独失配均构成服务端可证明的原任职失效。"""

    scenario = applying_import_scenario
    scenario.invalidate_original_assignment(invalidity)
    outcome = scenario.replacement_service.take_over(
        scenario.takeover_command,
        context=scenario.replacement_context,
    )
    assert outcome.status == "applied"
    assert outcome.succeeded_units == (1,)
    assert scenario.read_original_confirmer() == scenario.original_confirmer


def test_missing_original_assignment_is_independently_invalid() -> None:
    """纯判定锁定原任职缺失分支；真实库正常由复合外键阻止历史引用消失。"""

    assert application.MasterDataImportService._original_takeover_assignment_is_invalid(  # pyright: ignore[reportPrivateUsage]
        None,
        expected_person_id=uuid4(),
        expected_version=1,
        now=datetime.now(UTC),
    )


@pytest.mark.integration
def _retired_legacy_test_takeover_assignment_boundary_returns_absent_for_missing_original_assignment(  # noqa: E501
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """旧命令未引用指定任职或该行缺失时，窄锁函数只返回 absent 且不写入。"""

    scenario = applying_import_scenario
    actor = scenario.replacement_context.actor
    assert type(actor) is StaffActorContext
    before = scenario.snapshot_all_schools()
    with scenario.replacement_service._uow_factory.open(  # pyright: ignore[reportPrivateUsage]
        scenario.replacement_context
    ) as uow:
        assert (
            uow.imports.lock_takeover_assignment(
                actor.school_id,
                scenario.batch_id,
                scenario.old_idempotency_record_id,
                AssignmentId(uuid4()),
            )
            is None
        )
    assert scenario.snapshot_all_schools() == before


@pytest.mark.integration
def _retired_legacy_test_takeover_rejects_soft_anomaly_without_new_actor_reason(
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """正式 preflight 的 soft anomaly 必须由接管人独立填写继续理由。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    scenario = applying_import_scenario
    assert scenario.takeover_command.units[0].continue_reason is not None
    scenario.invalidate_original_assignment("status")
    command = scenario.takeover_command.model_copy(
        update={
            "units": (
                contracts.TakeoverImportUnitDecision(
                    sequence=scenario.takeover_command.units[0].sequence,
                    expected_unit_version=(
                        scenario.takeover_command.units[0].expected_unit_version
                    ),
                ),
            )
        }
    )
    before = scenario.snapshot_all_schools()
    with pytest.raises(application.MasterDataImportStateConflict):
        scenario.replacement_service.take_over(
            command,
            context=scenario.replacement_context,
        )
    assert scenario.snapshot_all_schools() == before


@pytest.mark.integration
def _retired_legacy_test_takeover_continues_remaining_units_and_preserves_predecessor_lineage(
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """正式三单元场景中已完成 attempt 保留，只有两个未完成 attempt 切给新命令。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    original_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    original_command, original_context = _ready_multi_ordinary_confirmation(
        original_service,
        import_actor_facts,
        marker=marker,
    )
    original_apply = original_service._apply_ordinary_person_unit

    def apply_first_then_stop(**kwargs: Any) -> bool:
        if kwargs["decision"].sequence == 1:
            return original_apply(**kwargs)
        raise _StopAfterActiveCommandCommit("首单元已提交，剩余单元停止")

    monkeypatch.setattr(
        original_service,
        "_apply_ordinary_person_unit",
        apply_first_then_stop,
    )
    with pytest.raises(_StopAfterActiveCommandCommit):
        original_service.confirm(original_command, context=original_context)
    preview = original_service.preview(
        original_command.batch_id,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-preview-mixed-takeover-{marker}",
        ),
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        before = connection.execute(
            """
            SELECT batch.active_idempotency_record_id,
                   batch.confirmed_by_user_id, batch.confirmed_by_person_id,
                   batch.confirmed_by_assignment_id, batch.confirmed_at,
                   array_agg(unit.last_attempt_idempotency_record_id ORDER BY unit.sequence),
                   array_agg(unit.last_attempt_completed ORDER BY unit.sequence),
                   array_agg(unit.status ORDER BY unit.sequence)
            FROM master_data_import_batches AS batch
            JOIN master_data_import_units AS unit
              ON unit.school_id = batch.school_id AND unit.batch_id = batch.id
            WHERE batch.school_id = %s AND batch.id = %s
            GROUP BY batch.id
            """,
            (import_actor_facts.school_id, original_command.batch_id),
        ).fetchone()
    assert before is not None
    old_record_id = before[0]
    assert before[5:] == (
        [old_record_id, old_record_id, old_record_id],
        [True, False, False],
        ["applied", "ready", "ready"],
    )
    original_confirmer = tuple(before[1:5])

    replacement_actor = _seed_same_school_import_actor(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
    )
    replacement_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    replacement_context = _command_context(
        replacement_actor,
        idempotency_key=f"task8-mixed-takeover-{marker}",
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            UPDATE staff_assignments
            SET status = 'rejected'
            WHERE school_id = %s AND id = %s
            """,
            (import_actor_facts.school_id, import_actor_facts.actor_assignment_id),
        )
        connection.execute(
            "UPDATE staff_assignment_versions SET revoked_at = clock_timestamp() "
            "WHERE school_id = %s AND assignment_id = %s AND revoked_at IS NULL",
            (import_actor_facts.school_id, import_actor_facts.actor_assignment_id),
        )
    takeover_command = contracts.TakeOverOrdinaryImportCommand(
        batch_id=preview.batch_id,
        expected_batch_version=preview.batch_version,
        confirmation_card_sha256=preview.confirmation_card_sha256,
        units=tuple(
            contracts.TakeoverImportUnitDecision(
                sequence=unit.sequence,
                expected_unit_version=unit.unit_version,
            )
            for unit in preview.units
            if unit.sequence in {2, 3}
        ),
        takeover_reason="原任职失效后仅接管尚未完成的两个单元",
    )
    outcome = replacement_service.take_over(
        takeover_command,
        context=replacement_context,
    )
    assert outcome.status == "applied"
    assert outcome.succeeded_units == (1, 2, 3)
    assert outcome.failed_units == ()
    assert outcome.executed_units == (1, 2, 3)
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        after = connection.execute(
            """
            SELECT batch.active_idempotency_record_id,
                   (SELECT takeover.new_idempotency_record_id
                    FROM master_data_import_takeovers AS takeover
                    WHERE takeover.school_id = batch.school_id
                      AND takeover.batch_id = batch.id
                    ORDER BY takeover.created_at DESC, takeover.id DESC
                    LIMIT 1),
                   batch.confirmed_by_user_id, batch.confirmed_by_person_id,
                   batch.confirmed_by_assignment_id, batch.confirmed_at,
                   array_agg(unit.last_attempt_idempotency_record_id ORDER BY unit.sequence),
                   array_agg(unit.last_attempt_completed ORDER BY unit.sequence),
                   array_agg(unit.status ORDER BY unit.sequence),
                   (SELECT count(*) FROM audit_logs
                    WHERE school_id = batch.school_id
                      AND event_type = 'master_data.import.taken_over')
            FROM master_data_import_batches AS batch
            JOIN master_data_import_units AS unit
              ON unit.school_id = batch.school_id AND unit.batch_id = batch.id
            WHERE batch.school_id = %s AND batch.id = %s
            GROUP BY batch.id
            """,
            (import_actor_facts.school_id, original_command.batch_id),
        ).fetchone()
    assert after is not None
    assert after[0] is None
    assert isinstance(after[1], UUID)
    assert tuple(after[2:6]) == original_confirmer
    assert after[6:] == (
        [old_record_id, after[1], after[1]],
        [True, True, True],
        ["applied", "applied", "applied"],
        1,
    )


@pytest.mark.integration
def _retired_legacy_test_takeover_retry_rejects_non_manifest_owner_without_first_outcome(
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """manifest 外已完成单元脱离 lineage 且没有自身首次结果时必须结果未知。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    original_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    original_command, original_context = _ready_multi_ordinary_confirmation(
        original_service,
        import_actor_facts,
        marker=f"non-manifest-owner-{marker}",
    )
    original_apply = original_service._apply_ordinary_person_unit

    def apply_first_then_stop(**kwargs: Any) -> bool:
        if kwargs["decision"].sequence == 1:
            return original_apply(**kwargs)
        raise _StopAfterActiveCommandCommit("首单元已提交，剩余单元停止")

    monkeypatch.setattr(
        original_service,
        "_apply_ordinary_person_unit",
        apply_first_then_stop,
    )
    with pytest.raises(_StopAfterActiveCommandCommit):
        original_service.confirm(original_command, context=original_context)
    preview = original_service.preview(
        original_command.batch_id,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-non-manifest-owner-preview-{marker}",
        ),
    )
    replacement_actor = _seed_same_school_import_actor(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
    )
    replacement_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    replacement_context = _command_context(
        replacement_actor,
        idempotency_key=f"task8-non-manifest-owner-takeover-{marker}",
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "UPDATE staff_assignments SET status = 'rejected' WHERE school_id = %s AND id = %s",
            (import_actor_facts.school_id, import_actor_facts.actor_assignment_id),
        )
        connection.execute(
            "UPDATE staff_assignment_versions SET revoked_at = clock_timestamp() "
            "WHERE school_id = %s AND assignment_id = %s AND revoked_at IS NULL",
            (import_actor_facts.school_id, import_actor_facts.actor_assignment_id),
        )
    takeover_command = contracts.TakeOverOrdinaryImportCommand(
        batch_id=preview.batch_id,
        expected_batch_version=preview.batch_version,
        confirmation_card_sha256=preview.confirmation_card_sha256,
        units=tuple(
            contracts.TakeoverImportUnitDecision(
                sequence=unit.sequence,
                expected_unit_version=unit.unit_version,
            )
            for unit in preview.units
            if unit.sequence in {2, 3}
        ),
        takeover_reason="原任职失效后仅接管尚未完成的两个单元",
    )
    original_continue = replacement_service._continue_taken_over_ordinary

    def stop_after_takeover_checkpoint(*_args: Any, **_kwargs: Any) -> Any:
        raise _StopAfterActiveCommandCommit("takeover checkpoint 已提交，continuation 尚未开始")

    monkeypatch.setattr(
        replacement_service,
        "_continue_taken_over_ordinary",
        stop_after_takeover_checkpoint,
    )
    with pytest.raises(_StopAfterActiveCommandCommit):
        replacement_service.take_over(takeover_command, context=replacement_context)
    monkeypatch.setattr(
        replacement_service,
        "_continue_taken_over_ordinary",
        original_continue,
    )

    rogue_record_id = uuid4()
    rogue_key_hash = sha256(f"rogue-owner-{marker}".encode()).hexdigest()
    with current_master_data_schema.admin_connection() as connection:
        connection.execute("SET session_replication_role = replica")
        old_record_id = connection.execute(
            "SELECT last_attempt_idempotency_record_id "
            "FROM public.master_data_import_units "
            "WHERE school_id = %s AND batch_id = %s AND sequence = 1",
            (import_actor_facts.school_id, original_command.batch_id),
        ).fetchone()
        assert old_record_id is not None
        connection.execute(
            """
            INSERT INTO public.command_idempotency_records (
                id, school_id, action_code, actor_user_id, actor_person_id,
                actor_context_type, actor_context_key, actor_assignment_id,
                actor_assignment_version, resource_type, resource_id,
                idempotency_key_hash, request_sha256, status,
                result_type, result_payload
            )
            SELECT %s, school_id, action_code, actor_user_id, actor_person_id,
                   actor_context_type, actor_context_key, actor_assignment_id,
                   actor_assignment_version, resource_type, resource_id,
                   %s, %s, 'succeeded', result_type, result_payload
            FROM public.command_idempotency_records
            WHERE school_id = %s AND id = %s
            """,
            (
                rogue_record_id,
                rogue_key_hash,
                "d" * 64,
                import_actor_facts.school_id,
                old_record_id[0],
            ),
        )
        connection.execute(
            "UPDATE public.master_data_import_units "
            "SET last_attempt_idempotency_record_id = %s "
            "WHERE school_id = %s AND batch_id = %s AND sequence = 1",
            (rogue_record_id, import_actor_facts.school_id, original_command.batch_id),
        )
        connection.execute("SET session_replication_role = origin")

    snapshot = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=None,
        context=replacement_context,
        school_id=import_actor_facts.school_id,
        batch_id=original_command.batch_id,
    )
    before_retry = snapshot.snapshot()
    with pytest.raises(application.MasterDataImportOutcomeVerificationFailed):
        replacement_service.take_over(takeover_command, context=replacement_context)
    assert snapshot.snapshot() == before_retry


@pytest.mark.integration
@pytest.mark.parametrize(
    "historical_tamper",
    (None, "version", "target"),
    ids=("valid", "applied-version", "applied-target"),
)
def _retired_legacy_test_takeover_of_active_resume_only_finalizes_resume_lineage_and_replays(
    historical_tamper: str | None,
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """Earlier confirm successes remain outside a later resume->takeover result."""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    original_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    confirm_command, confirm_context = _ready_multi_ordinary_confirmation(
        original_service,
        import_actor_facts,
        marker=f"resume-takeover-{marker}",
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        failed_display_name = connection.execute(
            "SELECT payload ->> 'display_name' FROM master_data_import_units "
            "WHERE school_id = %s AND batch_id = %s AND sequence = 2",
            (import_actor_facts.school_id, confirm_command.batch_id),
        ).fetchone()
    assert failed_display_name is not None
    _seed_confirmed_person_name(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
        display_name=str(failed_display_name[0]),
        external_reference=f"resume-takeover-existing-{marker}",
    )
    first = original_service.confirm(confirm_command, context=confirm_context)
    assert first.status == "partially_failed"
    assert first.succeeded_units == (1, 3)
    assert first.failed_units == (2,)

    failed_preview = original_service.preview(
        confirm_command.batch_id,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-resume-takeover-preview-{marker}",
        ),
    )
    failed_unit = next(unit for unit in failed_preview.units if unit.sequence == 2)
    resume_command = contracts.ConfirmImportBatchCommand(
        batch_id=failed_preview.batch_id,
        expected_batch_version=failed_preview.batch_version,
        confirmation_card_sha256=failed_preview.confirmation_card_sha256,
        decisions=(
            contracts.ImportUnitDecision(
                sequence=2,
                expected_unit_version=failed_unit.unit_version,
                continue_reason="已复核为不同人员，继续失败单元",
            ),
        ),
    )
    resume_context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-active-resume-takeover-{marker}",
    )

    def stop_after_resume_command_commit(**_kwargs: Any) -> bool:
        raise _StopAfterActiveCommandCommit("resume active-command COMMIT 后停止")

    monkeypatch.setattr(
        original_service,
        "_apply_ordinary_person_unit",
        stop_after_resume_command_commit,
    )
    with pytest.raises(_StopAfterActiveCommandCommit):
        original_service.resume(resume_command, context=resume_context)
    active_preview = original_service.preview(
        confirm_command.batch_id,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-active-resume-preview-{marker}",
        ),
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        before = connection.execute(
            """
            SELECT batch.active_idempotency_record_id,
                   array_agg(unit.last_attempt_idempotency_record_id ORDER BY unit.sequence),
                   array_agg(unit.last_attempt_completed ORDER BY unit.sequence)
            FROM master_data_import_batches AS batch
            JOIN master_data_import_units AS unit
              ON unit.school_id = batch.school_id AND unit.batch_id = batch.id
            WHERE batch.school_id = %s AND batch.id = %s
            GROUP BY batch.id
            """,
            (import_actor_facts.school_id, confirm_command.batch_id),
        ).fetchone()
        assert before is not None
        historical_first_outcome = connection.execute(
            """
            SELECT record.status, record.result_type,
                   record.result_payload ->> 'resource_id',
                   outcome.action_code, outcome.status, outcome.batch_version,
                   outcome.succeeded_units, outcome.failed_units, outcome.executed_units
            FROM command_idempotency_records AS record
            JOIN master_data_import_command_outcomes AS outcome
              ON outcome.school_id = record.school_id
             AND outcome.idempotency_record_id = record.id
            WHERE record.school_id = %s AND record.id = %s
            """,
            (import_actor_facts.school_id, before[1][0]),
        ).fetchone()
        connection.execute(
            "UPDATE staff_assignments SET status = 'rejected' WHERE school_id = %s AND id = %s",
            (import_actor_facts.school_id, import_actor_facts.actor_assignment_id),
        )
        connection.execute(
            "UPDATE staff_assignment_versions SET revoked_at = clock_timestamp() "
            "WHERE school_id = %s AND assignment_id = %s AND revoked_at IS NULL",
            (import_actor_facts.school_id, import_actor_facts.actor_assignment_id),
        )
    assert historical_first_outcome == (
        "succeeded",
        "master_data.import_outcome",
        str(confirm_command.batch_id),
        "master_data.import.ordinary_confirm",
        "partially_failed",
        7,
        [1, 3],
        [2],
        [1, 2, 3],
    )
    resume_record_id = before[0]
    assert isinstance(resume_record_id, UUID)
    assert before[1][1] == resume_record_id
    assert before[2] == [True, False, True]

    replacement_actor = _seed_same_school_import_actor(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
    )
    replacement_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    replacement_context = _command_context(
        replacement_actor,
        idempotency_key=f"task8-resume-replacement-{marker}",
    )
    active_failed_unit = next(unit for unit in active_preview.units if unit.sequence == 2)
    takeover_command = contracts.TakeOverOrdinaryImportCommand(
        batch_id=active_preview.batch_id,
        expected_batch_version=active_preview.batch_version,
        confirmation_card_sha256=active_preview.confirmation_card_sha256,
        units=(
            contracts.TakeoverImportUnitDecision(
                sequence=2,
                expected_unit_version=active_failed_unit.unit_version,
                continue_reason="接管人复核后继续 resume 失败单元",
            ),
        ),
        takeover_reason="原 resume 处理人任职失效，由现任人员继续",
    )
    original_continue = replacement_service._continue_taken_over_ordinary

    def stop_after_takeover_checkpoint(*_args: Any, **_kwargs: Any) -> Any:
        raise _StopAfterActiveCommandCommit("takeover checkpoint 已提交，continuation 尚未开始")

    monkeypatch.setattr(
        replacement_service,
        "_continue_taken_over_ordinary",
        stop_after_takeover_checkpoint,
    )
    with pytest.raises(_StopAfterActiveCommandCommit):
        replacement_service.take_over(takeover_command, context=replacement_context)
    monkeypatch.setattr(
        replacement_service,
        "_continue_taken_over_ordinary",
        original_continue,
    )
    if historical_tamper in {"version", "target"}:
        with current_master_data_schema.admin_connection() as connection:
            connection.execute("SET session_replication_role = replica")
            connection.execute(
                "UPDATE public.master_data_import_units "
                "SET version = CASE WHEN %s = 'version' THEN 99 ELSE version END, "
                "target_id = CASE WHEN %s = 'target' THEN %s ELSE target_id END "
                "WHERE school_id = %s AND batch_id = %s AND sequence = 1",
                (
                    historical_tamper,
                    historical_tamper,
                    uuid4(),
                    import_actor_facts.school_id,
                    confirm_command.batch_id,
                ),
            )
            connection.execute("SET session_replication_role = origin")
    snapshot = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=None,
        context=replacement_context,
        school_id=import_actor_facts.school_id,
        batch_id=confirm_command.batch_id,
    )
    before_retry = snapshot.snapshot()
    if historical_tamper is not None:
        with pytest.raises(application.MasterDataImportOutcomeVerificationFailed):
            replacement_service.take_over(takeover_command, context=replacement_context)
        assert snapshot.snapshot() == before_retry
        return

    outcome = replacement_service.take_over(takeover_command, context=replacement_context)
    replay = replacement_service.take_over(
        takeover_command,
        context=replacement_context,
    )
    assert replay == outcome
    assert outcome.status == "applied"
    assert outcome.succeeded_units == (2,)
    assert outcome.failed_units == ()
    assert outcome.executed_units == (2,)
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        row = connection.execute(
            """
            SELECT array_agg(unit.last_attempt_idempotency_record_id ORDER BY unit.sequence),
                   (SELECT count(*) FROM audit_logs
                    WHERE school_id = batch.school_id
                      AND event_type = 'master_data.person.imported'),
                   (SELECT count(*) FROM outbox_messages
                    WHERE school_id = batch.school_id
                      AND event_type = 'master_data.person.imported'),
                   (SELECT count(*) FROM master_data_import_command_outcomes
                    WHERE school_id = batch.school_id
                      AND action_code = 'master_data.import.ordinary_takeover'
                      AND batch_id = batch.id)
            FROM master_data_import_batches AS batch
            JOIN master_data_import_units AS unit
              ON unit.school_id = batch.school_id AND unit.batch_id = batch.id
            WHERE batch.school_id = %s AND batch.id = %s
            GROUP BY batch.id
            """,
            (import_actor_facts.school_id, confirm_command.batch_id),
        ).fetchone()
    assert row is not None
    assert row[0][0] != row[0][1] and row[0][1] != row[0][2]
    assert row[1:] == (3, 3, 1)


@pytest.mark.integration
@pytest.mark.parametrize(
    "historical_tamper",
    (None, "error"),
    ids=("valid", "failed-error"),
)
def _retired_legacy_test_takeover_of_active_resume_proves_historical_failed_unit_snapshot(
    historical_tamper: str | None,
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """A historical failed unit needs its first outcome's exact terminal facts."""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    original_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    confirm_command, confirm_context = _ready_multi_ordinary_confirmation(
        original_service,
        import_actor_facts,
        marker=f"failed-history-{marker}",
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        unit_rows = connection.execute(
            """
            SELECT sequence, payload ->> 'display_name', payload ->> 'external_reference'
            FROM master_data_import_units
            WHERE school_id = %s AND batch_id = %s AND sequence IN (1, 2)
            ORDER BY sequence
            """,
            (import_actor_facts.school_id, confirm_command.batch_id),
        ).fetchall()
    assert len(unit_rows) == 2
    for sequence, display_name, _external_reference in unit_rows:
        _seed_confirmed_person_name(
            current_master_data_schema,
            school_id=import_actor_facts.school_id,
            display_name=str(display_name),
            external_reference=f"failed-history-name-{marker}-{sequence}",
        )

    first = original_service.confirm(confirm_command, context=confirm_context)
    assert first.status == "partially_failed"
    assert first.succeeded_units == (3,)
    assert first.failed_units == (1, 2)
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        original_unit = connection.execute(
            """
            SELECT version, error_code, error_field_code,
                   last_attempt_idempotency_record_id
            FROM master_data_import_units
            WHERE school_id = %s AND batch_id = %s AND sequence = 1
            """,
            (import_actor_facts.school_id, confirm_command.batch_id),
        ).fetchone()
    assert original_unit is not None
    assert original_unit[1:3] == ("soft_anomaly_changed", "教职工姓名")
    confirm_record_id = original_unit[3]
    assert isinstance(confirm_record_id, UUID)

    failed_preview = original_service.preview(
        confirm_command.batch_id,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-failed-history-preview-{marker}",
        ),
    )
    failed_units = tuple(unit for unit in failed_preview.units if unit.sequence in (1, 2))
    assert tuple(unit.sequence for unit in failed_units) == (1, 2)
    _seed_confirmed_person_name(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
        display_name=f"编号占用人-{marker}",
        external_reference=str(unit_rows[0][2]),
    )
    resume_command = contracts.ConfirmImportBatchCommand(
        batch_id=failed_preview.batch_id,
        expected_batch_version=failed_preview.batch_version,
        confirmation_card_sha256=failed_preview.confirmation_card_sha256,
        decisions=tuple(
            contracts.ImportUnitDecision(
                sequence=unit.sequence,
                expected_unit_version=unit.unit_version,
                continue_reason="已复核同名人员，继续处理失败单元",
            )
            for unit in failed_units
        ),
    )
    resume_context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-failed-history-resume-{marker}",
    )
    original_apply = original_service._apply_ordinary_person_unit

    def fail_first_then_stop(**kwargs: Any) -> bool:
        decision = kwargs["decision"]
        if decision.sequence == 1:
            return original_apply(**kwargs)
        raise _StopAfterActiveCommandCommit("首个 resume 单元失败后停止")

    monkeypatch.setattr(
        original_service,
        "_apply_ordinary_person_unit",
        fail_first_then_stop,
    )
    with pytest.raises(_StopAfterActiveCommandCommit):
        original_service.resume(resume_command, context=resume_context)
    active_preview = original_service.preview(
        confirm_command.batch_id,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-failed-history-active-preview-{marker}",
        ),
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        attempt_rows = connection.execute(
            """
            SELECT sequence, status, last_attempt_idempotency_record_id,
                   last_attempt_completed, error_code, error_field_code
            FROM master_data_import_units
            WHERE school_id = %s AND batch_id = %s AND sequence IN (1, 2)
            ORDER BY sequence
            """,
            (import_actor_facts.school_id, confirm_command.batch_id),
        ).fetchall()
        connection.execute(
            "UPDATE staff_assignments SET status = 'rejected' WHERE school_id = %s AND id = %s",
            (import_actor_facts.school_id, import_actor_facts.actor_assignment_id),
        )
        connection.execute(
            "UPDATE staff_assignment_versions SET revoked_at = clock_timestamp() "
            "WHERE school_id = %s AND assignment_id = %s AND revoked_at IS NULL",
            (import_actor_facts.school_id, import_actor_facts.actor_assignment_id),
        )
    assert attempt_rows[0][1:] == (
        "failed",
        attempt_rows[0][2],
        True,
        "external_reference_conflict",
        "校内编号",
    )
    resume_record_id = attempt_rows[0][2]
    assert isinstance(resume_record_id, UUID)
    assert attempt_rows[1][2:] == (
        resume_record_id,
        False,
        "soft_anomaly_changed",
        "教职工姓名",
    )

    replacement_actor = _seed_same_school_import_actor(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
    )
    replacement_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    replacement_context = _command_context(
        replacement_actor,
        idempotency_key=f"task8-failed-history-takeover-{marker}",
    )
    active_failed_unit = next(unit for unit in active_preview.units if unit.sequence == 2)
    takeover_command = contracts.TakeOverOrdinaryImportCommand(
        batch_id=active_preview.batch_id,
        expected_batch_version=active_preview.batch_version,
        confirmation_card_sha256=active_preview.confirmation_card_sha256,
        units=(
            contracts.TakeoverImportUnitDecision(
                sequence=2,
                expected_unit_version=active_failed_unit.unit_version,
                continue_reason="接管人复核后继续尚未处理单元",
            ),
        ),
        takeover_reason="原 resume 处理人任职失效，由现任人员继续",
    )
    original_continue = replacement_service._continue_taken_over_ordinary

    def stop_after_takeover_checkpoint(*_args: Any, **_kwargs: Any) -> Any:
        raise _StopAfterActiveCommandCommit("takeover checkpoint 已提交，continuation 尚未开始")

    monkeypatch.setattr(
        replacement_service,
        "_continue_taken_over_ordinary",
        stop_after_takeover_checkpoint,
    )
    with pytest.raises(_StopAfterActiveCommandCommit):
        replacement_service.take_over(takeover_command, context=replacement_context)
    monkeypatch.setattr(
        replacement_service,
        "_continue_taken_over_ordinary",
        original_continue,
    )
    with current_master_data_schema.admin_connection() as connection:
        connection.execute("SET session_replication_role = replica")
        connection.execute(
            """
            UPDATE public.master_data_import_units
            SET version = %s,
                status = 'failed',
                target_id = NULL,
                target_version = NULL,
                error_code = %s,
                error_field_code = %s,
                decision_reason = NULL,
                last_attempt_idempotency_record_id = %s,
                last_attempt_completed = true
            WHERE school_id = %s AND batch_id = %s AND sequence = 1
            """,
            (
                original_unit[0],
                ("unit_transaction_failed" if historical_tamper == "error" else original_unit[1]),
                None if historical_tamper == "error" else original_unit[2],
                confirm_record_id,
                import_actor_facts.school_id,
                confirm_command.batch_id,
            ),
        )
        connection.execute("SET session_replication_role = origin")

    snapshot = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=None,
        context=replacement_context,
        school_id=import_actor_facts.school_id,
        batch_id=confirm_command.batch_id,
    )
    before_retry = snapshot.snapshot()
    if historical_tamper is not None:
        with pytest.raises(application.MasterDataImportOutcomeVerificationFailed):
            replacement_service.take_over(takeover_command, context=replacement_context)
        assert snapshot.snapshot() == before_retry
        return

    outcome = replacement_service.take_over(takeover_command, context=replacement_context)
    replay = replacement_service.take_over(takeover_command, context=replacement_context)
    assert replay == outcome
    assert outcome.status == "partially_failed"
    assert outcome.succeeded_units == (2,)
    assert outcome.failed_units == ()
    assert outcome.executed_units == (2,)


@pytest.mark.integration
def _retired_legacy_test_second_takeover_continues_complete_lineage_after_first_replacement_invalidates(  # noqa: E501
    current_master_data_schema: PostgresRoles,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """第一接管者只提交 checkpoint 后失效，第二接管者沿精确 provenance 完成首次结果。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    scenario = applying_import_scenario
    scenario.revoke_original_assignment()
    first_actor = scenario.replacement_context.actor
    assert type(first_actor) is StaffActorContext
    first_identity = scenario.replacement_service._takeover_idempotency_identity(  # pyright: ignore[reportPrivateUsage]
        scenario.takeover_command,
        scenario.replacement_context,
        first_actor,
    )
    first_checkpoint = scenario.replacement_service._execute_takeover_transaction(  # pyright: ignore[reportPrivateUsage]
        scenario.takeover_command,
        context=scenario.replacement_context,
        actor=first_actor,
        identity=first_identity,
    )
    assert first_checkpoint.status == "takeover_committed"

    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        changed = connection.execute(
            """
            UPDATE staff_assignments
            SET status = 'rejected'
            WHERE school_id = %s AND id = %s
            """,
            (scenario.school_id, first_actor.assignment_id),
        ).rowcount
        content_changed = connection.execute(
            "UPDATE staff_assignment_versions SET revoked_at = clock_timestamp() "
            "WHERE school_id = %s AND assignment_id = %s AND revoked_at IS NULL",
            (scenario.school_id, first_actor.assignment_id),
        ).rowcount
    assert changed == 1
    assert content_changed >= 1

    second_actor_facts = _seed_same_school_import_actor(
        current_master_data_schema,
        school_id=scenario.school_id,
    )
    second_service, _ = _import_service_and_uow_factory(scenario.session_factory)
    second_context = _command_context(
        second_actor_facts,
        idempotency_key=f"task8-second-lineage-takeover-{uuid4().hex}",
    )
    second_command = contracts.TakeOverOrdinaryImportCommand(
        batch_id=scenario.batch_id,
        expected_batch_version=first_checkpoint.batch_version_after,
        confirmation_card_sha256=scenario.takeover_command.confirmation_card_sha256,
        units=scenario.takeover_command.units,
        takeover_reason="第一位接管者任职失效，由第二位管理员继续同一数据库清单",
    )
    outcome = second_service.take_over(second_command, context=second_context)

    assert outcome.status == "applied"
    assert outcome.succeeded_units == (1,)
    assert outcome.failed_units == ()
    assert outcome.executed_units == (1,)
    assert scenario.read_original_confirmer() == scenario.original_confirmer
    assert scenario.read_takeover_count() == 2
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        row = connection.execute(
            """
            SELECT batch.active_idempotency_record_id,
                   batch.active_action_code,
                   batch.status,
                   unit.last_attempt_idempotency_record_id,
                   unit.last_attempt_completed,
                   array_agg(record.status ORDER BY takeover.created_at, takeover.id),
                   array_agg(record.result_payload ->> 'outcome_code'
                             ORDER BY takeover.created_at, takeover.id),
                   (SELECT count(*) FROM audit_logs AS audit
                    WHERE audit.school_id = batch.school_id
                      AND audit.event_type = 'master_data.import.taken_over'),
                   (SELECT count(*) FROM audit_logs AS audit
                    WHERE audit.school_id = batch.school_id
                      AND audit.event_type = 'master_data.person.imported'),
                   (SELECT count(*) FROM outbox_messages AS outbox
                    WHERE outbox.school_id = batch.school_id
                      AND outbox.event_type = 'master_data.person.imported')
            FROM master_data_import_batches AS batch
            JOIN master_data_import_units AS unit
              ON unit.school_id = batch.school_id AND unit.batch_id = batch.id
            JOIN master_data_import_takeovers AS takeover
              ON takeover.school_id = batch.school_id AND takeover.batch_id = batch.id
            JOIN command_idempotency_records AS record
              ON record.school_id = takeover.school_id
             AND record.id = takeover.previous_idempotency_record_id
            WHERE batch.school_id = %s AND batch.id = %s
            GROUP BY batch.id, unit.id
            """,
            (scenario.school_id, scenario.batch_id),
        ).fetchone()
    assert row is not None
    assert row[:5] == (None, None, "applied", row[3], True)
    assert row[3] not in {
        scenario.old_idempotency_record_id,
        first_checkpoint.new_idempotency_record_id,
    }
    assert row[5:] == (
        ["terminal_failure", "terminal_failure"],
        [
            "master_data.import.command_superseded",
            "master_data.import.command_superseded",
        ],
        2,
        1,
        1,
    )


@pytest.mark.integration
@pytest.mark.parametrize("lost_phase", ("takeover_checkpoint", "takeover_final"))
def _retired_legacy_test_takeover_commit_response_loss_uses_new_physical_probe_and_returns_first_outcome(  # noqa: E501
    lost_phase: str,
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """checkpoint 或最终 COMMIT 已成功但应答丢失时，只由新物理只读事务收敛。"""

    scenario = applying_import_scenario
    scenario.revoke_original_assignment()
    uow_type = application.SqlAlchemyMasterDataImportUoW
    probe_type = application.SqlAlchemyMasterDataImportProbeUoW
    original_commit = uow_type.commit
    original_probe_enter = probe_type.__enter__
    lost_once = False
    lost_backend_pid: int | None = None
    recovery_backend_pids: list[int] = []
    commit_reached = Barrier(2)
    release_response = Barrier(2)

    def commit_then_lose_response(self: Any) -> None:
        nonlocal lost_once, lost_backend_pid
        takeover_exists = bool(
            self.session.scalar(
                text(
                    "SELECT count(*) FROM master_data_import_takeovers "
                    "WHERE school_id = :school_id AND batch_id = :batch_id"
                ),
                {"school_id": scenario.school_id, "batch_id": scenario.batch_id},
            )
        )
        outcome_exists = bool(
            self.session.scalar(
                text(
                    "SELECT count(*) FROM master_data_import_command_outcomes "
                    "WHERE school_id = :school_id AND batch_id = :batch_id "
                    "AND action_code = 'master_data.import.ordinary_takeover'"
                ),
                {"school_id": scenario.school_id, "batch_id": scenario.batch_id},
            )
        )
        should_lose = takeover_exists and (
            (lost_phase == "takeover_checkpoint" and not outcome_exists)
            or (lost_phase == "takeover_final" and outcome_exists)
        )
        backend_pid = self.session.scalar(text("SELECT pg_backend_pid()"))
        assert isinstance(backend_pid, int)
        original_commit(self)
        if should_lose and not lost_once:
            lost_once = True
            lost_backend_pid = backend_pid
            commit_reached.wait(timeout=10)
            release_response.wait(timeout=10)
            raise CommitOutcomeUnknown(f"模拟接管 {lost_phase} COMMIT 成功后应答丢失")

    def capture_recovery_probe(self: Any) -> Any:
        entered = original_probe_enter(self)
        if lost_once:
            backend_pid = self._delegate.session.scalar(  # pyright: ignore[reportPrivateUsage]
                text("SELECT pg_backend_pid()")
            )
            assert isinstance(backend_pid, int)
            recovery_backend_pids.append(backend_pid)
        return entered

    monkeypatch.setattr(uow_type, "commit", commit_then_lose_response)
    monkeypatch.setattr(probe_type, "__enter__", capture_recovery_probe)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future = executor.submit(
            scenario.replacement_service.take_over,
            scenario.takeover_command,
            context=scenario.replacement_context,
        )
        commit_reached.wait(timeout=10)
        release_response.wait(timeout=10)
        first = first_future.result(timeout=10)
    replay = scenario.replacement_service.take_over(
        scenario.takeover_command,
        context=scenario.replacement_context,
    )

    assert lost_once is True
    assert lost_backend_pid is not None
    assert recovery_backend_pids
    assert lost_backend_pid not in recovery_backend_pids
    assert first == replay
    assert first.status == "applied"
    assert first.succeeded_units == (1,)
    assert scenario.read_takeover_count() == 1
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM school_person_names AS name
                 JOIN master_data_import_units AS unit
                   ON unit.school_id = name.school_id AND unit.target_id = name.person_id
                 WHERE unit.school_id = %s AND unit.batch_id = %s),
                (SELECT count(*) FROM audit_logs
                 WHERE school_id = %s AND event_type = 'master_data.person.imported'),
                (SELECT count(*) FROM outbox_messages
                 WHERE school_id = %s AND event_type = 'master_data.person.imported'),
                (SELECT count(*) FROM master_data_import_command_outcomes
                 WHERE school_id = %s AND batch_id = %s
                   AND action_code = 'master_data.import.ordinary_takeover')
            """,
            (
                scenario.school_id,
                scenario.batch_id,
                scenario.school_id,
                scenario.school_id,
                scenario.school_id,
                scenario.batch_id,
            ),
        ).fetchone()
    assert counts == (1, 1, 1, 1)


@pytest.mark.integration
@pytest.mark.parametrize("interruption", ("commit_unknown", "process_boundary"))
def _retired_legacy_test_takeover_retry_recovers_completed_manifest_unit_and_only_runs_remaining(
    interruption: str,
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """首个 manifest 单元物理提交后中断，同键只继续其余两个单元。"""

    scenario = _prepare_multi_unit_takeover(
        monkeypatch,
        current_master_data_schema,
        import_runtime_session_factory,
        import_actor_facts,
    )
    uow_type = application.SqlAlchemyMasterDataImportUoW
    probe_type = application.SqlAlchemyMasterDataImportProbeUoW
    service_type = application.MasterDataImportService
    original_commit = uow_type.commit
    original_probe_enter = probe_type.__enter__
    original_apply = service_type._apply_ordinary_person_unit
    commit_reached = Barrier(2)
    release_response = Barrier(2)
    lost_once = False
    lost_backend_pid: int | None = None
    recovery_backend_pids: list[int] = []
    applied_sequences: list[int] = []

    def record_apply(self: Any, **kwargs: Any) -> bool:
        applied_sequences.append(kwargs["decision"].sequence)
        return original_apply(self, **kwargs)

    def first_unit_commit_then_interrupt(self: Any) -> None:
        nonlocal lost_once, lost_backend_pid
        completed_manifest_units = int(
            self.session.scalar(
                text(
                    "SELECT count(*) FROM master_data_import_units AS unit "
                    "JOIN master_data_import_batches AS batch "
                    "ON batch.school_id = unit.school_id AND batch.id = unit.batch_id "
                    "WHERE unit.school_id = :school_id AND unit.batch_id = :batch_id "
                    "AND batch.active_action_code = 'master_data.import.ordinary_takeover' "
                    "AND unit.last_attempt_idempotency_record_id = "
                    "batch.active_idempotency_record_id "
                    "AND unit.last_attempt_completed IS TRUE"
                ),
                {"school_id": scenario.school_id, "batch_id": scenario.batch_id},
            )
            or 0
        )
        backend_pid = self.session.scalar(text("SELECT pg_backend_pid()"))
        assert isinstance(backend_pid, int)
        original_commit(self)
        if completed_manifest_units == 1 and not lost_once:
            lost_once = True
            lost_backend_pid = backend_pid
            commit_reached.wait(timeout=10)
            release_response.wait(timeout=10)
            if interruption == "commit_unknown":
                raise CommitOutcomeUnknown("首个接管单元 COMMIT 成功后应答丢失")
            raise _StopAfterActiveCommandCommit("首个接管单元提交后模拟进程边界")

    def capture_recovery_probe(self: Any) -> Any:
        entered = original_probe_enter(self)
        if lost_once:
            backend_pid = self._delegate.session.scalar(  # pyright: ignore[reportPrivateUsage]
                text("SELECT pg_backend_pid()")
            )
            assert isinstance(backend_pid, int)
            recovery_backend_pids.append(backend_pid)
        return entered

    monkeypatch.setattr(service_type, "_apply_ordinary_person_unit", record_apply)
    monkeypatch.setattr(uow_type, "commit", first_unit_commit_then_interrupt)
    monkeypatch.setattr(probe_type, "__enter__", capture_recovery_probe)
    first_outcome = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future = executor.submit(
            scenario.service.take_over,
            scenario.command,
            context=scenario.context,
        )
        commit_reached.wait(timeout=10)
        release_response.wait(timeout=10)
        if interruption == "commit_unknown":
            first_outcome = first_future.result(timeout=10)
        else:
            with pytest.raises(_StopAfterActiveCommandCommit):
                first_future.result(timeout=10)

    retry_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    retry_outcome = retry_service.take_over(scenario.command, context=scenario.context)
    replay = retry_service.take_over(scenario.command, context=scenario.context)

    assert lost_once is True
    assert lost_backend_pid is not None
    assert recovery_backend_pids
    assert lost_backend_pid not in recovery_backend_pids
    assert first_outcome is None or first_outcome == retry_outcome
    assert replay == retry_outcome
    assert retry_outcome.status == "applied"
    assert retry_outcome.succeeded_units == (1, 2, 3)
    assert retry_outcome.failed_units == ()
    assert retry_outcome.executed_units == (1, 2, 3)
    assert applied_sequences == [1, 2, 3]
    facts = scenario.facts()
    assert facts[:7] == ("applied", facts[1], 3, 0, 0, None, None)
    assert facts[8:] == (3, 3, 3, 1)


@pytest.mark.integration
@pytest.mark.parametrize(
    "tamper",
    ("target_version", "batch_counter", "failed_decision_reason"),
)
def _retired_legacy_test_takeover_retry_rejects_tampered_completed_manifest_evidence(
    tamper: str,
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """首单元提交后的单元或批次证据被改写时，不猜测进度或继续执行。"""

    scenario = _prepare_multi_unit_takeover(
        monkeypatch,
        current_master_data_schema,
        import_runtime_session_factory,
        import_actor_facts,
    )
    uow_type = application.SqlAlchemyMasterDataImportUoW
    service_type = application.MasterDataImportService
    original_commit = uow_type.commit
    original_apply = service_type._apply_ordinary_person_unit
    commit_reached = Barrier(2)
    release_response = Barrier(2)
    interrupted = False
    applied_sequences: list[int] = []

    if tamper == "failed_decision_reason":
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            external_reference = connection.execute(
                "SELECT payload ->> 'external_reference' "
                "FROM master_data_import_units "
                "WHERE school_id = %s AND batch_id = %s AND sequence = 1",
                (scenario.school_id, scenario.batch_id),
            ).fetchone()
        assert external_reference is not None
        _seed_confirmed_person_name(
            current_master_data_schema,
            school_id=scenario.school_id,
            display_name=f"失败理由占位教师-{uuid4().hex}",
            external_reference=str(external_reference[0]),
        )

    def record_apply(self: Any, **kwargs: Any) -> bool:
        applied_sequences.append(kwargs["decision"].sequence)
        return original_apply(self, **kwargs)

    def first_unit_commit_then_stop(self: Any) -> None:
        nonlocal interrupted
        completed_manifest_units = int(
            self.session.scalar(
                text(
                    "SELECT count(*) FROM master_data_import_units AS unit "
                    "JOIN master_data_import_batches AS batch "
                    "ON batch.school_id = unit.school_id AND batch.id = unit.batch_id "
                    "WHERE unit.school_id = :school_id AND unit.batch_id = :batch_id "
                    "AND batch.active_action_code = 'master_data.import.ordinary_takeover' "
                    "AND unit.last_attempt_idempotency_record_id = "
                    "batch.active_idempotency_record_id "
                    "AND unit.last_attempt_completed IS TRUE"
                ),
                {"school_id": scenario.school_id, "batch_id": scenario.batch_id},
            )
            or 0
        )
        original_commit(self)
        if completed_manifest_units == 1 and not interrupted:
            interrupted = True
            commit_reached.wait(timeout=10)
            release_response.wait(timeout=10)
            raise _StopAfterActiveCommandCommit("首个接管单元提交后模拟进程边界")

    monkeypatch.setattr(service_type, "_apply_ordinary_person_unit", record_apply)
    monkeypatch.setattr(uow_type, "commit", first_unit_commit_then_stop)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future = executor.submit(
            scenario.service.take_over,
            scenario.command,
            context=scenario.context,
        )
        commit_reached.wait(timeout=10)
        release_response.wait(timeout=10)
        with pytest.raises(_StopAfterActiveCommandCommit):
            first_future.result(timeout=10)
    assert applied_sequences == [1]

    with current_master_data_schema.admin_connection() as connection:
        connection.execute("SET session_replication_role = replica")
        if tamper == "target_version":
            connection.execute(
                "UPDATE public.master_data_import_units SET target_version = 2 "
                "WHERE school_id = %s AND batch_id = %s AND sequence = 1",
                (scenario.school_id, scenario.batch_id),
            )
        elif tamper == "batch_counter":
            connection.execute(
                "UPDATE public.master_data_import_batches "
                "SET succeeded_units = 0, failed_units = 1 "
                "WHERE school_id = %s AND id = %s",
                (scenario.school_id, scenario.batch_id),
            )
        else:
            connection.execute(
                "UPDATE public.master_data_import_units "
                "SET decision_reason = '被篡改的失败理由' "
                "WHERE school_id = %s AND batch_id = %s AND sequence = 1",
                (scenario.school_id, scenario.batch_id),
            )
        connection.execute("SET session_replication_role = origin")
    before_retry = scenario.facts()
    retry_service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    with pytest.raises(application.MasterDataImportOutcomeVerificationFailed):
        retry_service.take_over(scenario.command, context=scenario.context)
    assert scenario.facts() == before_retry
    assert applied_sequences == [1]


@pytest.mark.integration
def _retired_legacy_test_takeover_commit_unknown_without_durable_evidence_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """An unknown COMMIT with no durable checkpoint must never be guessed successful."""

    scenario = applying_import_scenario
    scenario.revoke_original_assignment()
    before = scenario.snapshot_all_schools()
    uow_type = application.SqlAlchemyMasterDataImportUoW
    probe_type = application.SqlAlchemyMasterDataImportProbeUoW
    original_commit = uow_type.commit
    original_probe_enter = probe_type.__enter__
    commit_reached = Barrier(2)
    release_response = Barrier(2)
    lost_once = False
    lost_backend_pid: int | None = None
    recovery_backend_pids: list[int] = []

    def rollback_then_report_unknown(self: Any) -> None:
        nonlocal lost_once, lost_backend_pid
        takeover_exists = bool(
            self.session.scalar(
                text(
                    "SELECT count(*) FROM master_data_import_takeovers "
                    "WHERE school_id = :school_id AND batch_id = :batch_id"
                ),
                {"school_id": scenario.school_id, "batch_id": scenario.batch_id},
            )
        )
        if takeover_exists and not lost_once:
            backend_pid = self.session.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(backend_pid, int)
            self.session.rollback()
            lost_once = True
            lost_backend_pid = backend_pid
            commit_reached.wait(timeout=10)
            release_response.wait(timeout=10)
            raise CommitOutcomeUnknown("模拟接管 COMMIT 未形成 durable checkpoint")
        original_commit(self)

    def capture_recovery_probe(self: Any) -> Any:
        entered = original_probe_enter(self)
        if lost_once:
            backend_pid = self._delegate.session.scalar(  # pyright: ignore[reportPrivateUsage]
                text("SELECT pg_backend_pid()")
            )
            assert isinstance(backend_pid, int)
            recovery_backend_pids.append(backend_pid)
        return entered

    monkeypatch.setattr(uow_type, "commit", rollback_then_report_unknown)
    monkeypatch.setattr(probe_type, "__enter__", capture_recovery_probe)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(
            scenario.replacement_service.take_over,
            scenario.takeover_command,
            context=scenario.replacement_context,
        )
        commit_reached.wait(timeout=10)
        release_response.wait(timeout=10)
        with pytest.raises(application.MasterDataImportOutcomeVerificationFailed):
            result.result(timeout=10)

    assert lost_once is True
    assert lost_backend_pid is not None
    assert recovery_backend_pids
    assert lost_backend_pid not in recovery_backend_pids
    assert scenario.snapshot_all_schools() == before


@pytest.mark.integration
@pytest.mark.parametrize(
    "availability",
    ["confirmed", "busy", "business_trip", "ordinary_leave"],
)
def _retired_legacy_test_effective_original_assignment_cannot_be_taken_over(
    availability: str,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    scenario = applying_import_scenario
    scenario.set_original_availability(availability)
    command = scenario.command_with_takeover_reason(f"原处理人当前处于{availability}状态，请求接管")
    before = scenario.snapshot()
    with pytest.raises(application.MasterDataImportStateConflict):
        scenario.replacement_service.take_over(
            command,
            context=scenario.replacement_context,
        )
    assert scenario.snapshot() == before


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation",
    ["confirmation_card", "batch_version", "unit_version", "cross_school_batch"],
)
def _retired_legacy_test_takeover_rejects_changed_card_unit_version_or_cross_school_target(
    mutation: str,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    scenario = applying_import_scenario
    scenario.revoke_original_assignment()
    command = scenario.command_with_mutation(mutation)
    before = scenario.snapshot_all_schools()
    with pytest.raises(
        (
            application.MasterDataImportVersionConflict,
            application.MasterDataImportStateConflict,
        )
    ):
        scenario.replacement_service.take_over(
            command,
            context=scenario.replacement_context,
        )
    assert scenario.snapshot_all_schools() == before


@pytest.mark.integration
@pytest.mark.parametrize("current_actor_violation", ["guardian", "unauthorized", "stale"])
def _retired_legacy_test_takeover_definer_boundary_rejects_invalid_current_actor_without_writes(
    current_actor_violation: str,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    scenario = applying_import_scenario
    scenario.revoke_original_assignment()
    context = scenario.replacement_context
    expected_error: type[BaseException]
    if current_actor_violation == "guardian":
        guardian = _issue_guardian_actor_context(
            account_user_id=scenario.replacement_context.actor.account_user_id,
            school_id=SchoolId(scenario.school_id),
            person_id=PersonId(scenario.replacement_context.actor.person_id),
            guardian_relationship_id=GuardianRelationshipId(uuid4()),
            relationship_version=1,
            authorization_version=1,
        )
        context = compose_command_context(
            guardian,
            uuid4(),
            f"task8-takeover-guardian-{uuid4().hex}",
        )
        expected_error = application.MasterDataImportContextRequired
    else:
        replacement_actor = scenario.replacement_context.actor
        assert type(replacement_actor) is StaffActorContext
        replacement_assignment_id = replacement_actor.assignment_id
        with scenario.roles.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            if current_actor_violation == "unauthorized":
                connection.execute(
                    """
                    UPDATE staff_assignments
                    SET semantic_role_code = 'teacher'
                    WHERE school_id = %s AND id = %s
                    """,
                    (scenario.school_id, replacement_assignment_id),
                )
                connection.execute(
                    """
                    UPDATE staff_assignment_versions
                    SET semantic_role_code = 'teacher'
                    WHERE school_id = %s AND assignment_id = %s
                    """,
                    (scenario.school_id, replacement_assignment_id),
                )
                expected_error = application.MasterDataImportPermissionDenied
            else:
                connection.execute(
                    """
                    UPDATE staff_assignments
                    SET version = version + 1
                    WHERE school_id = %s AND id = %s
                    """,
                    (scenario.school_id, replacement_assignment_id),
                )
                expected_error = application.MasterDataImportContextRequired

    before = scenario.snapshot_all_schools()
    with pytest.raises(expected_error):
        scenario.replacement_service.take_over(
            scenario.takeover_command,
            context=context,
        )
    assert scenario.snapshot_all_schools() == before


def _takeover_context_with_assignment_version(
    scenario: ApplyingImportScenario,
    *,
    assignment_version: int,
) -> CommandExecutionContext:
    actor = scenario.replacement_context.actor
    assert type(actor) is StaffActorContext
    versioned_actor = _issue_staff_actor_context(
        account_user_id=actor.account_user_id,
        school_id=actor.school_id,
        person_id=actor.person_id,
        assignment_id=actor.assignment_id,
        assignment_version=assignment_version,
        authorization_version=actor.authorization_version,
    )
    return compose_command_context(
        versioned_actor,
        uuid4(),
        f"task8-takeover-temporal-{uuid4().hex}",
    )


def _seed_replacement_assignment_role_slice(
    scenario: ApplyingImportScenario,
    *,
    boundary_offset: timedelta,
) -> CommandExecutionContext:
    actor = scenario.replacement_context.actor
    assert type(actor) is StaffActorContext
    with scenario.roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        boundary = connection.execute(
            "SELECT clock_timestamp() + %s::interval",
            (f"{boundary_offset.total_seconds()} seconds",),
        ).fetchone()
        assert boundary is not None
        effective_from = boundary[0]
        closed = connection.execute(
            """
            UPDATE staff_assignment_versions
            SET effective_to = %s
            WHERE school_id = %s AND assignment_id = %s AND target_version = 1
            """,
            (effective_from, scenario.school_id, actor.assignment_id),
        ).rowcount
        assert closed == 1
        inserted = connection.execute(
            """
            INSERT INTO staff_assignment_versions (
                id, school_id, assignment_id, person_id, organization_unit_id,
                position_title, semantic_role_code, assignment_kind,
                appointment_source, approval_reference,
                effective_from, effective_to, planned_valid_to, revoked_at,
                status, target_version, source_ref, source_id, import_batch_id
            )
            SELECT %s, school_id, assignment_id, person_id, organization_unit_id,
                   '普通教师', 'teacher', assignment_kind,
                   appointment_source, approval_reference,
                   %s, NULL, NULL, NULL,
                   'confirmed', 2, source_ref, source_id, NULL
            FROM staff_assignment_versions
            WHERE school_id = %s AND assignment_id = %s AND target_version = 1
            """,
            (uuid4(), effective_from, scenario.school_id, actor.assignment_id),
        ).rowcount
        assert inserted == 1
        projected = connection.execute(
            """
            UPDATE staff_assignments
            SET position_title = '普通教师', semantic_role_code = 'teacher',
                valid_from = %s, version = 2
            WHERE school_id = %s AND id = %s
            """,
            (effective_from, scenario.school_id, actor.assignment_id),
        ).rowcount
        assert projected == 1
    return _takeover_context_with_assignment_version(scenario, assignment_version=2)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("boundary_offset", "allowed"),
    ((timedelta(hours=1), True), (timedelta(seconds=-1), False)),
    ids=("before-future-role-change", "after-role-change"),
)
def _retired_legacy_test_takeover_definer_uses_authoritative_assignment_slice_at_database_time(
    boundary_offset: timedelta,
    allowed: bool,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    scenario = applying_import_scenario
    context = _seed_replacement_assignment_role_slice(
        scenario,
        boundary_offset=boundary_offset,
    )
    service = scenario.replacement_service
    actor = context.actor
    assert type(actor) is StaffActorContext
    with service._uow_factory.open(context) as uow:  # pyright: ignore[reportPrivateUsage]
        if allowed:
            assert (
                uow.imports.lock_takeover_source(
                    actor.school_id,
                    scenario.batch_id,
                    scenario.old_idempotency_record_id,
                )
                is not None
            )
        else:
            with pytest.raises(ProgrammingError) as raised:
                uow.imports.lock_takeover_source(
                    actor.school_id,
                    scenario.batch_id,
                    scenario.old_idempotency_record_id,
                )
            assert getattr(raised.value.orig, "sqlstate", None) == "42501"
    if allowed:
        scenario.revoke_original_assignment()
        outcome = scenario.replacement_service.take_over(
            scenario.takeover_command,
            context=context,
        )
        assert outcome.status in {"applied", "partially_failed"}


@pytest.mark.integration
def _retired_legacy_test_takeover_definer_revalidates_terminal_lineage_after_context_binding(
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    scenario = applying_import_scenario
    actor = scenario.replacement_context.actor
    assert type(actor) is StaffActorContext
    with scenario.roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        changed = connection.execute(
            "UPDATE staff_assignments SET version = 2 WHERE school_id = %s AND id = %s",
            (scenario.school_id, actor.assignment_id),
        ).rowcount
        assert changed == 1
    context = _takeover_context_with_assignment_version(scenario, assignment_version=2)
    service = scenario.replacement_service
    with service._uow_factory.open(context) as uow:  # pyright: ignore[reportPrivateUsage]
        with scenario.roles.migration_connection() as connection, connection.transaction():
            connection.execute("SET LOCAL ROLE campus_ai_db_owner")
            database_now = connection.execute("SELECT clock_timestamp() ").fetchone()
            assert database_now is not None
            confirmer_user_id, confirmer_person_id, confirmer_assignment_id, _ = (
                scenario.original_confirmer
            )
            assert isinstance(confirmer_user_id, int)
            assert isinstance(confirmer_person_id, UUID)
            assert isinstance(confirmer_assignment_id, UUID)
            _seed_formal_structure_lineage(
                connection,
                school_id=scenario.school_id,
                user_id=confirmer_user_id,
                person_id=confirmer_person_id,
                assignment_id=confirmer_assignment_id,
                fact_kind="assignment",
                fact_source_key=f"task8-terminal-{uuid4().hex}",
                target_id=actor.assignment_id,
                effective_at=database_now[0] - timedelta(seconds=1),
                operation="end",
                target_version=2,
                expected_version=1,
            )
        with pytest.raises(ProgrammingError) as raised:
            uow.imports.lock_takeover_source(
                actor.school_id,
                scenario.batch_id,
                scenario.old_idempotency_record_id,
            )
        assert getattr(raised.value.orig, "sqlstate", None) == "42501"


@pytest.mark.integration
def _retired_legacy_test_two_replacement_actors_compete_for_one_takeover_with_real_connections(
    monkeypatch: pytest.MonkeyPatch,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """不同任职、不同幂等键同时接管时，数据库只允许一个 durable checkpoint。"""

    scenario = applying_import_scenario
    scenario.revoke_original_assignment()
    second_actor = _seed_same_school_import_actor(
        scenario.roles,
        school_id=scenario.school_id,
    )
    second_service, _ = _import_service_and_uow_factory(scenario.session_factory)
    second_context = _command_context(
        second_actor,
        idempotency_key=f"task8-second-takeover-{uuid4().hex}",
    )
    assert scenario.replacement_context.idempotency_key != second_context.idempotency_key

    start = Barrier(3)
    worker_state = local()
    backend_pids: dict[str, int] = {}
    original_enter = application.SqlAlchemyMasterDataImportUoW.__enter__

    def capture_real_backend(self: Any) -> Any:
        entered = original_enter(self)
        label = getattr(worker_state, "label", None)
        if label is not None:
            pid = self.session.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(pid, int)
            backend_pids[label] = pid
        return entered

    monkeypatch.setattr(
        application.SqlAlchemyMasterDataImportUoW,
        "__enter__",
        capture_real_backend,
    )

    def compete(label: str, service: Any, context: CommandExecutionContext) -> tuple[str, Any]:
        worker_state.label = label
        start.wait(timeout=5)
        try:
            return "success", service.take_over(scenario.takeover_command, context=context)
        except (
            application.MasterDataImportStateConflict,
            application.MasterDataImportVersionConflict,
            application.MasterDataImportIdempotencyConflict,
        ) as exc:
            return "conflict", exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(
                compete,
                "first",
                scenario.replacement_service,
                scenario.replacement_context,
            ),
            executor.submit(compete, "second", second_service, second_context),
        )
        start.wait(timeout=5)
        outcomes = tuple(future.result(timeout=10) for future in futures)

    assert sorted(outcome[0] for outcome in outcomes) == ["conflict", "success"]
    assert len(set(backend_pids.values())) == 2
    winner = next(value for kind, value in outcomes if kind == "success")
    assert winner.status == "applied"
    assert winner.succeeded_units == (1,)
    assert scenario.read_takeover_count() == 1
    with scenario.roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        row = connection.execute(
            """
            SELECT batch.active_idempotency_record_id,
                   takeover.new_idempotency_record_id,
                   old_record.status,
                   unit.last_attempt_idempotency_record_id,
                   (SELECT count(*) FROM command_idempotency_records
                    WHERE school_id = batch.school_id
                      AND action_code = 'master_data.import.ordinary_takeover'
                      AND resource_id = batch.id::text),
                   (SELECT count(*) FROM audit_logs
                    WHERE school_id = batch.school_id
                      AND event_type = 'master_data.import.taken_over')
            FROM master_data_import_batches AS batch
            JOIN master_data_import_units AS unit
              ON unit.school_id = batch.school_id AND unit.batch_id = batch.id
            JOIN command_idempotency_records AS old_record
              ON old_record.school_id = batch.school_id AND old_record.id = %s
            JOIN master_data_import_takeovers AS takeover
              ON takeover.school_id = batch.school_id AND takeover.batch_id = batch.id
            WHERE batch.school_id = %s AND batch.id = %s AND unit.sequence = 1
            """,
            (
                scenario.old_idempotency_record_id,
                scenario.school_id,
                scenario.batch_id,
            ),
        ).fetchone()
    assert row is not None
    takeover_record_id = row[1]
    assert isinstance(takeover_record_id, UUID)
    assert row == (
        None,
        takeover_record_id,
        "terminal_failure",
        takeover_record_id,
        1,
        1,
    )


@pytest.mark.integration
def _retired_legacy_test_ordinary_same_key_two_executor_before_unit_lock_converges(
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """同键执行者同时进入单元事务时，真实行锁后必须返回同一首次结果。"""

    service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    command, context = _ready_single_ordinary_confirmation(
        service,
        import_actor_facts,
        marker=marker,
    )
    person_count_before, name_count_before = _school_person_name_counts(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
    )
    enter_unit = Barrier(2)
    original_apply = service._apply_ordinary_person_unit

    def enter_real_unit_together(**kwargs: Any) -> bool:
        enter_unit.wait(timeout=5)
        return original_apply(**kwargs)

    monkeypatch.setattr(service, "_apply_ordinary_person_unit", enter_real_unit_together)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(
            executor.submit(service.confirm, command, context=context) for _ in range(2)
        )
        outcomes = tuple(future.result(timeout=10) for future in futures)

    assert outcomes[0] == outcomes[1]
    facts = _ordinary_same_key_facts(
        current_master_data_schema,
        actor=import_actor_facts,
        batch_id=command.batch_id,
        external_reference=marker,
        request_id=context.request_id,
    )
    assert facts[0] == person_count_before + 1
    assert facts[1] == 1
    assert facts[2] == name_count_before + 1
    assert facts[3] == 1
    assert facts[4:14] == (1, 1, 1, 1, "applied", 1, 0, 0, "applied", True)


@pytest.mark.integration
def _retired_legacy_test_ordinary_same_key_two_executor_after_unit_commit_converges(
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """落后执行者在单元已提交且最终化未写时，必须读取同一单元完成事实。"""

    service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    command, context = _ready_single_ordinary_confirmation(
        service,
        import_actor_facts,
        marker=marker,
    )
    person_count_before, name_count_before = _school_person_name_counts(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
    )
    unit_committed = Event()
    lagging_finished = Event()
    worker_state = local()
    original_apply = service._apply_ordinary_person_unit

    def hold_finalization_around_lagging_unit(**kwargs: Any) -> bool:
        if worker_state.role == "leading":
            result = original_apply(**kwargs)
            unit_committed.set()
            if not lagging_finished.wait(timeout=5):
                raise AssertionError("落后执行者未在单元提交后完成收敛")
            return result
        return original_apply(**kwargs)

    def execute(role: str) -> Any:
        worker_state.role = role
        if role == "lagging" and not unit_committed.wait(timeout=5):
            raise AssertionError("领先执行者未提交普通导入单元")
        try:
            return service.confirm(command, context=context)
        finally:
            if role == "lagging":
                lagging_finished.set()

    monkeypatch.setattr(
        service,
        "_apply_ordinary_person_unit",
        hold_finalization_around_lagging_unit,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(execute, "leading"),
            executor.submit(execute, "lagging"),
        )
        outcomes = tuple(future.result(timeout=10) for future in futures)

    assert outcomes[0] == outcomes[1]
    facts = _ordinary_same_key_facts(
        current_master_data_schema,
        actor=import_actor_facts,
        batch_id=command.batch_id,
        external_reference=marker,
        request_id=context.request_id,
    )
    assert facts[0] == person_count_before + 1
    assert facts[1] == 1
    assert facts[2] == name_count_before + 1
    assert facts[3] == 1
    assert facts[4:14] == (1, 1, 1, 1, "applied", 1, 0, 0, "applied", True)


@pytest.mark.integration
def _retired_legacy_test_ordinary_completed_unit_failure_recorder_is_same_record_idempotent(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """失败记录器只对同一已完成 record 幂等，其他内容、键或归属保持失败关闭。"""

    application = importlib.import_module("app.platform.master_data_imports.application")
    service, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    command, context = _ready_single_ordinary_confirmation(
        service,
        import_actor_facts,
        marker=marker,
    )
    outcome = service.confirm(command, context=context)
    assert outcome.status == "applied"
    record_id = _ordinary_same_key_facts(
        current_master_data_schema,
        actor=import_actor_facts,
        batch_id=command.batch_id,
        external_reference=marker,
        request_id=context.request_id,
    )[14]
    assert isinstance(record_id, UUID)
    full_facts = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=context,
        school_id=import_actor_facts.school_id,
        batch_id=command.batch_id,
    )
    before = full_facts.snapshot()

    service._record_unit_transaction_failure(
        batch_id=command.batch_id,
        sequence=1,
        context=context,
        eligible_statuses=("ready",),
        idempotency_record_id=record_id,
    )
    assert full_facts.snapshot() == before

    with pytest.raises(application.MasterDataImportStateConflict):
        service._record_unit_transaction_failure(
            batch_id=command.batch_id,
            sequence=1,
            context=context,
            eligible_statuses=("ready",),
            idempotency_record_id=uuid4(),
        )
    assert full_facts.snapshot() == before

    with pytest.raises(application.MasterDataImportStateConflict):
        service._apply_ordinary_person_unit(
            batch_id=command.batch_id,
            decision=command.decisions[0],
            context=context,
            eligible_statuses=("ready",),
            idempotency_record_id=uuid4(),
        )
    assert full_facts.snapshot() == before

    changed_content = command.model_copy(
        update={"expected_batch_version": command.expected_batch_version + 1}
    )
    with pytest.raises(application.MasterDataImportIdempotencyConflict):
        service.confirm(changed_content, context=context)
    assert full_facts.snapshot() == before

    with pytest.raises(application.MasterDataImportStateConflict):
        service.confirm(
            command,
            context=_command_context(
                import_actor_facts,
                idempotency_key=f"task8-confirm-different-key-{marker}",
            ),
        )
    assert full_facts.snapshot() == before


@pytest.mark.integration
def _retired_legacy_test_ordinary_import_resumes_only_failed_atomic_unit(
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """普通导入单元独立提交；一个版本冲突不能回滚其他成功单元，续跑也不能重复成功项。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    uow_factory = application.SqlAlchemyMasterDataImportUoWFactory(
        session_factory=import_runtime_session_factory,
        envelope_factory=_envelope_factory,
        lock_timeout_ms=750,
    )
    service = application.MasterDataImportService(
        uow_factory=uow_factory,
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    workbook = _workbook_bytes(
        ("姓名", "编号"),
        ("测试教师甲", f"{marker}-1"),
        ("测试教师乙", f"{marker}-2"),
        ("测试教师丙", f"{marker}-3"),
        title="教师名单",
    )
    source = contracts.ProtectedImportSourceRef(
        opaque_ref=f"protected-{marker}",
        source_sha256=sha256(workbook).hexdigest(),
        filename_summary="teacher-roster.xlsx",
        size_bytes=len(workbook),
        worksheet_names=("教师名单",),
    )
    mapping = contracts.ColumnMappingDraft(
        mapping_version=1,
        entries=(
            contracts.ColumnMappingEntry(
                source_column_index=1,
                source_header="姓名",
                target_field_code="person.display_name",
            ),
            contracts.ColumnMappingEntry(
                source_column_index=2,
                source_header="编号",
                target_field_code="person.external_reference",
            ),
        ),
    )
    units = tuple(
        contracts.PersonImportUnitDraft(
            row_number=sequence + 1,
            display_name=display_name,
            external_reference=f"{marker}-{sequence}",
            valid_from=datetime.now(UTC).replace(microsecond=0),
        )
        for sequence, display_name in enumerate(("测试教师甲", "测试教师乙", "测试教师丙"), start=1)
    )
    staged = service.stage_ordinary(
        contracts.StageOrdinaryPersonImportCommand(
            source=source,
            worksheet_name="教师名单",
            mapping=mapping,
            units=units,
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-ordinary-{marker}",
        ),
    )
    assert staged.status == "mapped"
    ready = service.preflight(
        contracts.PreflightImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            expected_mapping_version=1,
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-preflight-ordinary-{marker}",
        ),
    )
    assert ready.status == "ready_for_confirmation"

    uow_type = application.SqlAlchemyMasterDataImportUoW
    original_commit = uow_type.commit
    seeded_same_name_once = False

    def applying_commit_then_create_same_name_race(self: Any) -> None:
        nonlocal seeded_same_name_once
        is_initial_applying = (
            self.session.scalar(
                text(
                    "SELECT status FROM master_data_import_batches "
                    "WHERE school_id = :school_id AND id = :batch_id"
                ),
                {
                    "school_id": import_actor_facts.school_id,
                    "batch_id": ready.batch_id,
                },
            )
            == "applying"
        )
        formal_count = self.session.scalar(
            text(
                "SELECT count(*) FROM school_person_names "
                "WHERE school_id = :school_id AND source_ref LIKE :source_pattern"
            ),
            {
                "school_id": import_actor_facts.school_id,
                "source_pattern": f"{marker}-%",
            },
        )
        original_commit(self)
        if is_initial_applying and formal_count == 0 and not seeded_same_name_once:
            seeded_same_name_once = True
            _seed_confirmed_person_name(
                current_master_data_schema,
                school_id=import_actor_facts.school_id,
                display_name="测试教师乙",
                external_reference=f"race-existing-{marker}",
            )

    monkeypatch.setattr(uow_type, "commit", applying_commit_then_create_same_name_race)

    first = service.confirm(
        contracts.ConfirmImportBatchCommand(
            batch_id=ready.batch_id,
            expected_batch_version=ready.batch_version,
            confirmation_card_sha256=ready.confirmation_card_sha256,
            decisions=tuple(
                contracts.ImportUnitDecision(
                    sequence=unit.sequence,
                    expected_unit_version=unit.unit_version,
                )
                for unit in ready.units
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-confirm-ordinary-{marker}",
        ),
    )
    assert first.status == "partially_failed"
    assert seeded_same_name_once is True
    assert first.succeeded_units == (1, 3)
    assert first.failed_units == (2,)
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        first_confirmer = connection.execute(
            """
            SELECT confirmed_by_user_id, confirmed_by_person_id,
                   confirmed_by_assignment_id, confirmed_at
            FROM master_data_import_batches
            WHERE school_id = %s AND id = %s
            """,
            (import_actor_facts.school_id, first.batch_id),
        ).fetchone()
    assert first_confirmer is not None

    refreshed = service.preview(
        first.batch_id,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-preview-ordinary-{marker}",
        ),
    )
    failed = next(unit for unit in refreshed.units if unit.sequence == 2)
    replay = service.resume(
        contracts.ConfirmImportBatchCommand(
            batch_id=refreshed.batch_id,
            expected_batch_version=refreshed.batch_version,
            confirmation_card_sha256=refreshed.confirmation_card_sha256,
            decisions=(
                contracts.ImportUnitDecision(
                    sequence=2,
                    expected_unit_version=failed.unit_version,
                    continue_reason="已核对为不同人员",
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-resume-ordinary-{marker}",
        ),
    )
    assert replay.status == "applied"
    assert replay.executed_units == (2,)

    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        resumed_confirmer = connection.execute(
            """
            SELECT confirmed_by_user_id, confirmed_by_person_id,
                   confirmed_by_assignment_id, confirmed_at
            FROM master_data_import_batches
            WHERE school_id = %s AND id = %s
            """,
            (import_actor_facts.school_id, replay.batch_id),
        ).fetchone()
        formal_count = connection.execute(
            """
            SELECT count(*) FROM school_person_names
            WHERE school_id = %s AND source_kind = 'person_external_reference'
              AND source_ref LIKE %s
            """,
            (import_actor_facts.school_id, f"{marker}-%"),
        ).fetchone()
        audit_count = connection.execute(
            """
            SELECT count(*) FROM audit_logs
            WHERE school_id = %s AND event_type = 'master_data.person.imported'
              AND reason = 'excel.person-import-confirmed'
            """,
            (import_actor_facts.school_id,),
        ).fetchone()
    assert resumed_confirmer == first_confirmer
    assert formal_count == (3,)
    assert audit_count == (3,)


@pytest.mark.integration
def _retired_legacy_test_ordinary_final_commit_response_loss_replays_exact_first_outcome(
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """普通导入最终结果提交应答丢失后，同键重放不能重复执行原子单元。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    valid_from = datetime.now(UTC).replace(microsecond=0)
    content = _workbook_bytes(("姓名", "编号"), ("普通响应丢失教师", marker), title="教师名单")
    staged = service.stage_ordinary(
        contracts.StageOrdinaryPersonImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"protected-ordinary-response-loss-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="teacher-roster.xlsx",
                size_bytes=len(content),
                worksheet_names=("教师名单",),
            ),
            worksheet_name="教师名单",
            mapping=contracts.ColumnMappingDraft(
                mapping_version=1,
                entries=(
                    contracts.ColumnMappingEntry(
                        source_column_index=1,
                        source_header="姓名",
                        target_field_code="person.display_name",
                    ),
                    contracts.ColumnMappingEntry(
                        source_column_index=2,
                        source_header="编号",
                        target_field_code="person.external_reference",
                    ),
                ),
            ),
            units=(
                contracts.PersonImportUnitDraft(
                    row_number=2,
                    display_name="普通响应丢失教师",
                    external_reference=marker,
                    valid_from=valid_from,
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-ordinary-response-loss-{marker}",
        ),
    )
    ready = service.preflight(
        contracts.PreflightImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            expected_mapping_version=1,
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-preflight-ordinary-response-loss-{marker}",
        ),
    )
    command = contracts.ConfirmImportBatchCommand(
        batch_id=ready.batch_id,
        expected_batch_version=ready.batch_version,
        confirmation_card_sha256=ready.confirmation_card_sha256,
        decisions=(
            contracts.ImportUnitDecision(
                sequence=1,
                expected_unit_version=ready.units[0].unit_version,
            ),
        ),
    )
    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-confirm-ordinary-response-loss-{marker}",
    )
    uow_type = application.SqlAlchemyMasterDataImportUoW
    original_commit = uow_type.commit
    lost_once = False

    def final_commit_then_lose_response(self: Any) -> None:
        nonlocal lost_once
        outcome_exists = bool(
            self.session.scalar(
                text(
                    "SELECT count(*) FROM master_data_import_command_outcomes "
                    "WHERE school_id = :school_id AND batch_id = :batch_id"
                ),
                {
                    "school_id": import_actor_facts.school_id,
                    "batch_id": ready.batch_id,
                },
            )
        )
        original_commit(self)
        if outcome_exists and not lost_once:
            lost_once = True
            raise CommitOutcomeUnknown("模拟普通导入最终 COMMIT 成功后应答丢失")

    monkeypatch.setattr(uow_type, "commit", final_commit_then_lose_response)
    first = service.confirm(command, context=context)
    replay = service.confirm(command, context=context)

    assert lost_once is True
    assert replay == first
    assert first.succeeded_units == (1,)
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        count = connection.execute(
            """
            SELECT count(*) FROM school_person_names
            WHERE school_id = %s AND source_ref = %s
            """,
            (import_actor_facts.school_id, marker),
        ).fetchone()
    assert count == (1,)


@pytest.mark.integration
@pytest.mark.parametrize("lost_phase", ("applying_transition", "unit_commit"))
def _retired_legacy_test_ordinary_in_progress_response_loss_requires_same_key_recovery(
    lost_phase: str,
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """未形成终态时先显式报结果未知，只有原命令同键重试可精确收敛。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    content = _workbook_bytes(("姓名", "编号"), ("普通恢复教师", marker), title="教师名单")
    staged = service.stage_ordinary(
        contracts.StageOrdinaryPersonImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"protected-ordinary-recovery-{lost_phase}-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="teacher-roster.xlsx",
                size_bytes=len(content),
                worksheet_names=("教师名单",),
            ),
            worksheet_name="教师名单",
            mapping=contracts.ColumnMappingDraft(
                mapping_version=1,
                entries=(
                    contracts.ColumnMappingEntry(
                        source_column_index=1,
                        source_header="姓名",
                        target_field_code="person.display_name",
                    ),
                    contracts.ColumnMappingEntry(
                        source_column_index=2,
                        source_header="编号",
                        target_field_code="person.external_reference",
                    ),
                ),
            ),
            units=(
                contracts.PersonImportUnitDraft(
                    row_number=2,
                    display_name="普通恢复教师",
                    external_reference=marker,
                    valid_from=datetime.now(UTC).replace(microsecond=0),
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-ordinary-recovery-{lost_phase}-{marker}",
        ),
    )
    ready = service.preflight(
        contracts.PreflightImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            expected_mapping_version=1,
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-preflight-ordinary-recovery-{lost_phase}-{marker}",
        ),
    )
    command = contracts.ConfirmImportBatchCommand(
        batch_id=ready.batch_id,
        expected_batch_version=ready.batch_version,
        confirmation_card_sha256=ready.confirmation_card_sha256,
        decisions=(
            contracts.ImportUnitDecision(
                sequence=1,
                expected_unit_version=ready.units[0].unit_version,
            ),
        ),
    )
    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-confirm-ordinary-recovery-{lost_phase}-{marker}",
    )
    uow_type = application.SqlAlchemyMasterDataImportUoW
    original_commit = uow_type.commit
    lost_once = False

    def commit_selected_phase_then_lose_response(self: Any) -> None:
        nonlocal lost_once
        batch_is_applying = (
            self.session.scalar(
                text(
                    "SELECT status FROM master_data_import_batches "
                    "WHERE school_id = :school_id AND id = :batch_id"
                ),
                {
                    "school_id": import_actor_facts.school_id,
                    "batch_id": ready.batch_id,
                },
            )
            == "applying"
        )
        person_exists = bool(
            self.session.scalar(
                text(
                    "SELECT count(*) FROM school_person_names "
                    "WHERE school_id = :school_id AND source_ref = :source_ref"
                ),
                {"school_id": import_actor_facts.school_id, "source_ref": marker},
            )
        )
        outcome_exists = bool(
            self.session.scalar(
                text(
                    "SELECT count(*) FROM master_data_import_command_outcomes "
                    "WHERE school_id = :school_id AND batch_id = :batch_id"
                ),
                {
                    "school_id": import_actor_facts.school_id,
                    "batch_id": ready.batch_id,
                },
            )
        )
        should_lose = (
            batch_is_applying
            and not outcome_exists
            and (
                (lost_phase == "applying_transition" and not person_exists)
                or (lost_phase == "unit_commit" and person_exists)
            )
        )
        original_commit(self)
        if should_lose and not lost_once:
            lost_once = True
            raise CommitOutcomeUnknown(f"模拟普通导入 {lost_phase} COMMIT 应答丢失")

    monkeypatch.setattr(uow_type, "commit", commit_selected_phase_then_lose_response)
    with pytest.raises(application.MasterDataImportOutcomeVerificationFailed):
        service.confirm(command, context=context)
    assert lost_once is True

    if lost_phase == "applying_transition":
        interrupted = service.preview(
            ready.batch_id,
            context=_command_context(
                import_actor_facts,
                idempotency_key=f"task8-preview-hijack-rejected-{marker}",
            ),
        )
        hijack_command = contracts.ConfirmImportBatchCommand(
            batch_id=interrupted.batch_id,
            expected_batch_version=interrupted.batch_version,
            confirmation_card_sha256=interrupted.confirmation_card_sha256,
            decisions=(
                contracts.ImportUnitDecision(
                    sequence=1,
                    expected_unit_version=interrupted.units[0].unit_version,
                ),
            ),
        )
        with pytest.raises(application.MasterDataImportStateConflict):
            service.resume(
                hijack_command,
                context=_command_context(
                    import_actor_facts,
                    idempotency_key=f"task8-resume-hijack-rejected-{marker}",
                ),
            )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            with current_master_data_schema.migration_connection() as connection:
                connection.execute("SET ROLE campus_ai_db_owner")
                connection.execute(
                    """
                    UPDATE master_data_import_units
                    SET payload = jsonb_set(payload, '{display_name}', to_jsonb(%s::text))
                    WHERE school_id = %s AND batch_id = %s AND sequence = 1
                    """,
                    ("事务间篡改教师", import_actor_facts.school_id, ready.batch_id),
                )

    recovered = service.confirm(command, context=context)
    replay = service.confirm(command, context=context)
    assert recovered.status == "applied"
    assert recovered.succeeded_units == (1,)
    assert replay == recovered
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM school_person_names
               WHERE school_id = %s AND source_ref = %s),
              (SELECT count(*) FROM audit_logs
               WHERE school_id = %s AND event_type = 'master_data.person.imported'),
              (SELECT count(*) FROM outbox_messages
               WHERE school_id = %s AND aggregate_id IN (
                   SELECT person_id FROM school_person_names
                   WHERE school_id = %s AND source_ref = %s
               ))
            """,
            (
                import_actor_facts.school_id,
                marker,
                import_actor_facts.school_id,
                import_actor_facts.school_id,
                import_actor_facts.school_id,
                marker,
            ),
        ).fetchone()
    assert counts == (1, 1, 1)


@pytest.mark.integration
def _retired_legacy_test_authoritative_calendar_is_invisible_until_atomic_publish(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """权威校历草稿不能抢占当前版本；发布事务必须同时结束旧版本并开放新版本。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    old_calendar_id = uuid4()
    new_valid_from = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO school_calendar_versions (
                id, school_id, display_name, timezone_name,
                valid_from, source_kind, source_ref, status, version
            ) VALUES (%s, %s, '旧校历', 'Asia/Shanghai', %s,
                      'manual', %s, 'published', 1)
            """,
            (
                old_calendar_id,
                import_actor_facts.school_id,
                new_valid_from - timedelta(days=30),
                f"old-calendar-{marker}",
            ),
        )
        connection.commit()

    content = _workbook_bytes(("日期", "类型"), ("2026-09-01", "上课"), title="校历")
    staged = service.stage_authoritative_calendar(
        contracts.StageAuthoritativeCalendarImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"calendar-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="school-calendar.xlsx",
                size_bytes=len(content),
                worksheet_names=("校历",),
            ),
            draft=contracts.AuthoritativeCalendarDraft(
                display_name="新校历",
                timezone_name="Asia/Shanghai",
                valid_from=new_valid_from,
                valid_to=None,
                days=(
                    contracts.CalendarDayDraft(
                        local_date=date(2026, 9, 1),
                        day_kind="working",
                        actual_start_time=time(8, 0),
                        actual_end_time=time(17, 30),
                        note=None,
                    ),
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-calendar-{marker}",
        ),
    )
    assert staged.status == "ready_for_confirmation"
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        visible_before = connection.execute(
            """
            SELECT id, display_name FROM school_calendar_versions
            WHERE school_id = %s AND status = 'published'
              AND valid_from <= clock_timestamp()
              AND (valid_to IS NULL OR valid_to > clock_timestamp())
            """,
            (import_actor_facts.school_id,),
        ).fetchall()
    assert visible_before == [(old_calendar_id, "旧校历")]

    published = service.publish_calendar(
        contracts.ConfirmImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            confirmation_card_sha256=staged.confirmation_card_sha256,
            decisions=(),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-publish-calendar-{marker}",
        ),
    )
    assert published.status == "published"

    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        visible_after = connection.execute(
            """
            SELECT id, display_name FROM school_calendar_versions
            WHERE school_id = %s AND status = 'published'
              AND valid_from <= clock_timestamp()
              AND (valid_to IS NULL OR valid_to > clock_timestamp())
            """,
            (import_actor_facts.school_id,),
        ).fetchall()
        old_end = connection.execute(
            "SELECT valid_to FROM school_calendar_versions WHERE id = %s",
            (old_calendar_id,),
        ).fetchone()
        audit_count = connection.execute(
            """
            SELECT count(*) FROM audit_logs
            WHERE school_id = %s AND event_type = 'master_data.calendar.published'
            """,
            (import_actor_facts.school_id,),
        ).fetchone()
    assert len(visible_after) == 1
    assert visible_after[0][0] != old_calendar_id
    assert visible_after[0][1] == "新校历"
    assert old_end == (new_valid_from,)
    assert audit_count == (1,)


@pytest.mark.integration
def _retired_legacy_test_calendar_publish_rejects_owner_draft_and_day_tampering(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """发布必须重验 owner 草稿逐字段事实，不能只相信导入摘要、版本和日期条数。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    content = _workbook_bytes(("日期", "类型"), ("2026-09-03", "上课"), title="校历")
    staged = service.stage_authoritative_calendar(
        contracts.StageAuthoritativeCalendarImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"calendar-tamper-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="school-calendar.xlsx",
                size_bytes=len(content),
                worksheet_names=("校历",),
            ),
            draft=contracts.AuthoritativeCalendarDraft(
                display_name="待发布校历",
                timezone_name="Asia/Shanghai",
                valid_from=datetime.now(UTC).replace(microsecond=0),
                valid_to=None,
                days=(
                    contracts.CalendarDayDraft(
                        local_date=date(2026, 9, 3),
                        day_kind="working",
                        actual_start_time=time(8, 0),
                        actual_end_time=time(17, 30),
                        note="正常上课",
                    ),
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-calendar-tamper-{marker}",
        ),
    )
    command = contracts.ConfirmImportBatchCommand(
        batch_id=staged.batch_id,
        expected_batch_version=staged.batch_version,
        confirmation_card_sha256=staged.confirmation_card_sha256,
        decisions=(),
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        draft_row = connection.execute(
            """
            SELECT target_id FROM master_data_import_units
            WHERE school_id = %s AND batch_id = %s AND sequence = 1
            """,
            (import_actor_facts.school_id, staged.batch_id),
        ).fetchone()
        assert draft_row is not None
        draft_id = draft_row[0]
        connection.execute(
            "UPDATE school_calendar_versions SET display_name = '被篡改校历' WHERE id = %s",
            (draft_id,),
        )
        connection.commit()

    with pytest.raises(application.MasterDataImportVersionConflict, match="校历草稿"):
        service.publish_calendar(
            command,
            context=_command_context(
                import_actor_facts,
                idempotency_key=f"task8-publish-calendar-tampered-version-{marker}",
            ),
        )

    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "UPDATE school_calendar_versions SET display_name = '待发布校历' WHERE id = %s",
            (draft_id,),
        )
        connection.execute(
            """
            UPDATE school_calendar_days
            SET day_kind = 'rest', actual_start_time = NULL, actual_end_time = NULL
            WHERE school_id = %s AND calendar_version_id = %s
            """,
            (import_actor_facts.school_id, draft_id),
        )
        connection.commit()

    with pytest.raises(application.MasterDataImportVersionConflict, match="校历草稿"):
        service.publish_calendar(
            command,
            context=_command_context(
                import_actor_facts,
                idempotency_key=f"task8-publish-calendar-tampered-day-{marker}",
            ),
        )

    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        unchanged = connection.execute(
            """
            SELECT calendar.status, batch.status, unit.status,
                   (SELECT count(*) FROM audit_logs
                    WHERE school_id = %s AND event_type = 'master_data.calendar.published')
            FROM school_calendar_versions AS calendar
            JOIN master_data_import_units AS unit ON unit.target_id = calendar.id
            JOIN master_data_import_batches AS batch
              ON batch.school_id = unit.school_id AND batch.id = unit.batch_id
            WHERE calendar.id = %s
            """,
            (import_actor_facts.school_id, draft_id),
        ).fetchone()
    assert unchanged == ("draft", "ready_for_confirmation", "ready", 0)


@pytest.mark.integration
def _retired_legacy_test_calendar_publish_response_loss_and_same_key_replay_return_first_outcome(
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """校历整体发布与幂等终态同事务提交，应答丢失后只读取回首次结果。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    content = _workbook_bytes(("日期", "类型"), ("2026-09-04", "上课"), title="校历")
    staged = service.stage_authoritative_calendar(
        contracts.StageAuthoritativeCalendarImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"calendar-response-loss-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="school-calendar.xlsx",
                size_bytes=len(content),
                worksheet_names=("校历",),
            ),
            draft=contracts.AuthoritativeCalendarDraft(
                display_name="响应丢失校历",
                timezone_name="Asia/Shanghai",
                valid_from=datetime.now(UTC).replace(microsecond=0),
                valid_to=None,
                days=(
                    contracts.CalendarDayDraft(
                        local_date=date(2026, 9, 4),
                        day_kind="working",
                        actual_start_time=time(8, 0),
                        actual_end_time=time(17, 30),
                        note=None,
                    ),
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-calendar-response-loss-{marker}",
        ),
    )
    command = contracts.ConfirmImportBatchCommand(
        batch_id=staged.batch_id,
        expected_batch_version=staged.batch_version,
        confirmation_card_sha256=staged.confirmation_card_sha256,
        decisions=(),
    )
    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-publish-calendar-response-loss-{marker}",
    )
    uow_type = application.SqlAlchemyMasterDataImportUoW
    original_commit = uow_type.commit
    lost_once = False

    def commit_then_lose_response(self: Any) -> None:
        nonlocal lost_once
        original_commit(self)
        if not lost_once:
            lost_once = True
            raise CommitOutcomeUnknown("模拟校历 COMMIT 成功后应答丢失")

    monkeypatch.setattr(uow_type, "commit", commit_then_lose_response)
    first = service.publish_calendar(command, context=context)
    replay = service.publish_calendar(command, context=context)

    assert lost_once is True
    assert replay == first
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM school_calendar_versions
               WHERE school_id = %s AND status = 'published' AND display_name = '响应丢失校历'),
              (SELECT count(*) FROM audit_logs
               WHERE school_id = %s AND event_type = 'master_data.calendar.published'),
              (SELECT count(*) FROM outbox_messages
               WHERE school_id = %s AND aggregate_id IN (
                   SELECT id FROM school_calendar_versions
                   WHERE school_id = %s AND display_name = '响应丢失校历'
               ))
            """,
            (
                import_actor_facts.school_id,
                import_actor_facts.school_id,
                import_actor_facts.school_id,
                import_actor_facts.school_id,
            ),
        ).fetchone()
    assert counts == (1, 1, 1)


@pytest.mark.integration
def _retired_legacy_test_same_idempotency_key_with_different_manual_content_fails_closed(
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """同一任职和幂等键不能被另一份候选内容复用，也不能泄露底层唯一约束。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    idempotency_key = f"task8-same-key-{uuid4().hex}"
    first = contracts.StageManualPersonUnitCommand(
        source_reference=f"manual-{uuid4().hex}",
        unit=contracts.PersonImportUnitDraft(
            row_number=1,
            display_name="测试教师甲",
            external_reference=f"T-{uuid4().hex}",
            valid_from=datetime.now(UTC).replace(microsecond=0),
        ),
    )
    service.stage_manual(
        first,
        context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
    )
    changed = first.model_copy(
        update={"unit": first.unit.model_copy(update={"display_name": "测试教师乙"})}
    )

    with pytest.raises(application.MasterDataImportIdempotencyConflict):
        service.stage_manual(
            changed,
            context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
        )


@pytest.mark.integration
def _retired_legacy_test_manual_stage_replay_returns_the_first_preview_after_confirmation(
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """响应丢失后的同键重放必须返回首次草稿结果，不能伪装成新的状态查询。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    stage_key = f"task8-replay-stage-manual-{uuid4().hex}"
    command = contracts.StageManualPersonUnitCommand(
        source_reference=f"manual-{uuid4().hex}",
        unit=contracts.PersonImportUnitDraft(
            row_number=1,
            display_name="重放测试教师",
            external_reference=f"T-{uuid4().hex}",
            valid_from=datetime.now(UTC).replace(microsecond=0),
        ),
    )
    first = service.stage_manual(
        command,
        context=_command_context(import_actor_facts, idempotency_key=stage_key),
    )
    service.confirm(
        contracts.ConfirmImportBatchCommand(
            batch_id=first.batch_id,
            expected_batch_version=first.batch_version,
            confirmation_card_sha256=first.confirmation_card_sha256,
            decisions=(),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-confirm-before-replay-{uuid4().hex}",
        ),
    )

    replay = service.stage_manual(
        command,
        context=_command_context(import_actor_facts, idempotency_key=stage_key),
    )

    assert replay == first
    assert replay.status == "ready_for_confirmation"
    assert replay.batch_version == 1


@pytest.mark.integration
def _retired_legacy_test_manual_confirm_response_loss_and_same_key_replay_return_first_outcome(
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """手工确认 COMMIT 已落库但应答丢失时，只读探测并重放完全相同的首次结果。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    staged = service.stage_manual(
        contracts.StageManualPersonUnitCommand(
            source_reference=f"manual-response-loss-{marker}",
            unit=contracts.PersonImportUnitDraft(
                row_number=1,
                display_name="响应丢失测试教师",
                external_reference=f"response-loss-{marker}",
                valid_from=datetime.now(UTC).replace(microsecond=0),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-manual-response-loss-{marker}",
        ),
    )
    command = contracts.ConfirmImportBatchCommand(
        batch_id=staged.batch_id,
        expected_batch_version=staged.batch_version,
        confirmation_card_sha256=staged.confirmation_card_sha256,
        decisions=(),
    )
    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-confirm-manual-response-loss-{marker}",
    )
    uow_type = application.SqlAlchemyMasterDataImportUoW
    original_commit = uow_type.commit
    lost_once = False

    def commit_then_lose_response(self: Any) -> None:
        nonlocal lost_once
        original_commit(self)
        if not lost_once:
            lost_once = True
            raise CommitOutcomeUnknown("模拟导入 COMMIT 成功后应答丢失")

    monkeypatch.setattr(uow_type, "commit", commit_then_lose_response)
    first = service.confirm(command, context=context)
    replay = service.confirm(command, context=context)

    assert lost_once is True
    assert replay == first
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM school_person_names
               WHERE school_id = %s AND source_ref = %s),
              (SELECT count(*) FROM audit_logs
               WHERE school_id = %s AND event_type = 'master_data.person.imported'),
              (SELECT count(*) FROM outbox_messages
               WHERE school_id = %s AND aggregate_id IN (
                   SELECT person_id FROM school_person_names
                   WHERE school_id = %s AND source_ref = %s
               ))
            """,
            (
                import_actor_facts.school_id,
                f"response-loss-{marker}",
                import_actor_facts.school_id,
                import_actor_facts.school_id,
                import_actor_facts.school_id,
                f"response-loss-{marker}",
            ),
        ).fetchone()
    assert counts == (1, 1, 1)


@pytest.mark.integration
def _retired_legacy_test_same_idempotency_key_binds_selected_ordinary_worksheet(
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """普通导入不能用同一幂等键悄悄切换受保护工作表。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    command = contracts.StageOrdinaryPersonImportCommand(
        source=contracts.ProtectedImportSourceRef(
            opaque_ref=f"protected-{marker}",
            source_sha256=sha256(marker.encode("utf-8")).hexdigest(),
            filename_summary="synthetic-roster.xlsx",
            size_bytes=1024,
            worksheet_names=("教师名单", "备用名单"),
        ),
        worksheet_name="教师名单",
        mapping=contracts.ColumnMappingDraft(
            mapping_version=1,
            entries=(
                contracts.ColumnMappingEntry(
                    source_column_index=1,
                    source_header="姓名",
                    target_field_code="person.display_name",
                ),
                contracts.ColumnMappingEntry(
                    source_column_index=2,
                    source_header="校内编号",
                    target_field_code="person.external_reference",
                ),
            ),
        ),
        units=(
            contracts.PersonImportUnitDraft(
                row_number=2,
                display_name="工作表绑定教师",
                external_reference=f"T-{marker}",
                valid_from=datetime.now(UTC).replace(microsecond=0),
            ),
        ),
    )
    idempotency_key = f"task8-worksheet-binding-{marker}"
    service.stage_ordinary(
        command,
        context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
    )

    with pytest.raises(application.MasterDataImportIdempotencyConflict):
        service.stage_ordinary(
            command.model_copy(update={"worksheet_name": "备用名单"}),
            context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
        )


@pytest.mark.integration
def _retired_legacy_test_same_idempotency_key_binds_authoritative_source_metadata(
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """权威集合不能在同一幂等键下替换受保护来源元数据。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    source = contracts.ProtectedImportSourceRef(
        opaque_ref=f"protected-calendar-{marker}",
        source_sha256=sha256(marker.encode("utf-8")).hexdigest(),
        filename_summary="calendar-a.xlsx",
        size_bytes=1024,
        worksheet_names=("校历",),
    )
    command = contracts.StageAuthoritativeCalendarImportCommand(
        source=source,
        draft=contracts.AuthoritativeCalendarDraft(
            display_name="幂等来源校历",
            timezone_name="Asia/Shanghai",
            valid_from=datetime.now(UTC).replace(microsecond=0) + timedelta(days=30),
            valid_to=None,
            days=(
                contracts.CalendarDayDraft(
                    local_date=date.today() + timedelta(days=30),
                    day_kind="rest",
                    actual_start_time=None,
                    actual_end_time=None,
                    note="合成测试",
                ),
            ),
        ),
    )
    idempotency_key = f"task8-calendar-source-binding-{marker}"
    service.stage_authoritative_calendar(
        command,
        context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
    )

    with pytest.raises(application.MasterDataImportIdempotencyConflict):
        service.stage_authoritative_calendar(
            command.model_copy(
                update={"source": source.model_copy(update={"filename_summary": "calendar-b.xlsx"})}
            ),
            context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
        )


@pytest.mark.integration
def _retired_legacy_test_old_mapping_version_and_cross_school_batch_id_are_rejected(
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    other_import_actor_facts: ImportActorFacts,
) -> None:
    """预检不能沿用旧映射，另一所学校的合法管理员也不能发现或读取批次。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    content = _workbook_bytes(("姓名", "编号"), ("测试教师", marker), title="教师名单")
    staged = service.stage_ordinary(
        contracts.StageOrdinaryPersonImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"protected-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="teacher-roster.xlsx",
                size_bytes=len(content),
                worksheet_names=("教师名单",),
            ),
            worksheet_name="教师名单",
            mapping=contracts.ColumnMappingDraft(
                mapping_version=1,
                entries=(
                    contracts.ColumnMappingEntry(
                        source_column_index=1,
                        source_header="姓名",
                        target_field_code="person.display_name",
                    ),
                    contracts.ColumnMappingEntry(
                        source_column_index=2,
                        source_header="编号",
                        target_field_code="person.external_reference",
                    ),
                ),
            ),
            units=(
                contracts.PersonImportUnitDraft(
                    row_number=2,
                    display_name="测试教师",
                    external_reference=marker,
                    valid_from=datetime.now(UTC).replace(microsecond=0),
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-old-map-{marker}",
        ),
    )

    with pytest.raises(application.MasterDataImportVersionConflict):
        service.preflight(
            contracts.PreflightImportBatchCommand(
                batch_id=staged.batch_id,
                expected_batch_version=staged.batch_version,
                expected_mapping_version=2,
            ),
            context=_command_context(
                import_actor_facts,
                idempotency_key=f"task8-preflight-old-map-{marker}",
            ),
        )
    with pytest.raises(application.MasterDataImportStateConflict):
        service.preview(
            staged.batch_id,
            context=_command_context(
                other_import_actor_facts,
                idempotency_key=f"task8-cross-school-preview-{marker}",
            ),
        )


@pytest.mark.integration
def _retired_legacy_test_duplicate_confirmation_does_not_duplicate_formal_person(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """已经完成的确认不能再次执行，更不能生成第二个人员、审计或 Outbox。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    preview = service.stage_manual(
        contracts.StageManualPersonUnitCommand(
            source_reference=f"manual-{marker}",
            unit=contracts.PersonImportUnitDraft(
                row_number=1,
                display_name="测试教师",
                external_reference=marker,
                valid_from=datetime.now(UTC).replace(microsecond=0),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-duplicate-{marker}",
        ),
    )
    command = contracts.ConfirmImportBatchCommand(
        batch_id=preview.batch_id,
        expected_batch_version=preview.batch_version,
        confirmation_card_sha256=preview.confirmation_card_sha256,
        decisions=(),
    )
    service.confirm(
        command,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-confirm-duplicate-first-{marker}",
        ),
    )
    with pytest.raises(application.MasterDataImportStateConflict):
        service.confirm(
            command,
            context=_command_context(
                import_actor_facts,
                idempotency_key=f"task8-confirm-duplicate-second-{marker}",
            ),
        )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM school_person_names
                 WHERE school_id = %s AND source_kind = 'person_external_reference'
                   AND source_ref = %s),
                (SELECT count(*) FROM outbox_messages
                 WHERE school_id = %s AND event_type = 'master_data.person.imported'
                   AND aggregate_id IN (
                       SELECT person_id FROM school_person_names
                       WHERE school_id = %s AND source_ref = %s
                   ))
            """,
            (
                import_actor_facts.school_id,
                marker,
                import_actor_facts.school_id,
                import_actor_facts.school_id,
                marker,
            ),
        ).fetchone()
    assert counts == (1, 1)


@pytest.mark.integration
def _retired_legacy_test_manual_stage_surfaces_same_name_and_requires_bound_reason(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """手工录入与 Excel 使用同一中性同名规则，确认理由必须绑定预览单元版本。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    uow_factory = application.SqlAlchemyMasterDataImportUoWFactory(
        session_factory=import_runtime_session_factory,
        envelope_factory=_envelope_factory,
        lock_timeout_ms=750,
    )
    service = application.MasterDataImportService(
        uow_factory=uow_factory,
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    stage_key = f"task8-stage-manual-same-name-{marker}"
    valid_from = datetime.now(UTC).replace(microsecond=0)
    _seed_confirmed_person_name(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
        display_name="同名教师",
        external_reference=f"existing-manual-{marker}",
    )
    staged = service.stage_manual(
        contracts.StageManualPersonUnitCommand(
            source_reference=f"manual-same-name-{marker}",
            unit=contracts.PersonImportUnitDraft(
                row_number=1,
                display_name="同名教师",
                external_reference=f"new-manual-{marker}",
                valid_from=valid_from,
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=stage_key,
        ),
    )
    assert staged.units[0].soft_anomaly_codes == ("same_display_name",)
    replay = service.stage_manual(
        contracts.StageManualPersonUnitCommand(
            source_reference=f"manual-same-name-{marker}",
            unit=contracts.PersonImportUnitDraft(
                row_number=1,
                display_name="同名教师",
                external_reference=f"new-manual-{marker}",
                valid_from=valid_from,
            ),
        ),
        context=_command_context(import_actor_facts, idempotency_key=stage_key),
    )
    assert replay == staged
    without_reason = contracts.ConfirmImportBatchCommand(
        batch_id=staged.batch_id,
        expected_batch_version=staged.batch_version,
        confirmation_card_sha256=staged.confirmation_card_sha256,
        decisions=(),
    )
    with pytest.raises(application.MasterDataImportStateConflict):
        service.confirm(
            without_reason,
            context=_command_context(
                import_actor_facts,
                idempotency_key=f"task8-confirm-manual-same-name-no-reason-{marker}",
            ),
        )

    outcome = service.confirm(
        without_reason.model_copy(
            update={
                "decisions": (
                    contracts.ImportUnitDecision(
                        sequence=1,
                        expected_unit_version=staged.units[0].unit_version,
                        continue_reason="已核对为不同人员",
                    ),
                )
            }
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-confirm-manual-same-name-with-reason-{marker}",
        ),
    )
    assert outcome.status == "applied"
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        facts = connection.execute(
            """
            SELECT unit.decision_reason, audit.after_summary -> 'fact_codes'
            FROM master_data_import_units AS unit
            JOIN audit_logs AS audit
              ON audit.school_id = unit.school_id
             AND audit.target_id = unit.target_id::text
             AND audit.event_type = 'master_data.person.imported'
            WHERE unit.school_id = %s AND unit.batch_id = %s
            """,
            (import_actor_facts.school_id, staged.batch_id),
        ).fetchone()
    assert facts == (
        "已核对为不同人员",
        ["person.imported", "person.same_name_manually_continued"],
    )
    mutation_context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-mutate-final-reason-{marker}",
    )
    with uow_factory.open(mutation_context) as uow:
        with pytest.raises(DBAPIError, match="terminal master data import decision is immutable"):
            uow.session.execute(
                text(
                    "UPDATE master_data_import_units SET decision_reason = '被篡改理由' "
                    "WHERE school_id = :school_id AND batch_id = :batch_id"
                ),
                {
                    "school_id": import_actor_facts.school_id,
                    "batch_id": staged.batch_id,
                },
            )

    with uow_factory.open(mutation_context) as uow:
        with pytest.raises(DBAPIError, match="terminal master data import decision is immutable"):
            uow.session.execute(
                text(
                    "UPDATE master_data_import_units "
                    "SET status = 'failed', error_code = 'terminal-detour' "
                    "WHERE school_id = :school_id AND batch_id = :batch_id"
                ),
                {
                    "school_id": import_actor_facts.school_id,
                    "batch_id": staged.batch_id,
                },
            )


@pytest.mark.integration
@pytest.mark.parametrize("tamper_kind", ("soft_anomalies", "payload"))
def _retired_legacy_test_manual_confirm_recomputes_locked_payload_and_confirmation_card(
    tamper_kind: str,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """预览后的允许更新列或 Owner 载荷破坏都不能绕过固定确认卡。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    display_name = "确认卡同名教师" if tamper_kind == "soft_anomalies" else "确认卡教师"
    if tamper_kind == "soft_anomalies":
        _seed_confirmed_person_name(
            current_master_data_schema,
            school_id=import_actor_facts.school_id,
            display_name=display_name,
            external_reference=f"existing-card-{marker}",
        )
    valid_from = datetime.now(UTC).replace(microsecond=0)
    staged = service.stage_manual(
        contracts.StageManualPersonUnitCommand(
            source_reference=f"manual-card-integrity-{marker}",
            unit=contracts.PersonImportUnitDraft(
                row_number=1,
                display_name=display_name,
                external_reference=f"card-integrity-{marker}",
                valid_from=valid_from,
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-card-integrity-{marker}",
        ),
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        if tamper_kind == "soft_anomalies":
            connection.execute(
                "UPDATE master_data_import_units SET soft_anomalies = '[]'::jsonb "
                "WHERE school_id = %s AND batch_id = %s",
                (import_actor_facts.school_id, staged.batch_id),
            )
        else:
            connection.execute(
                """
                UPDATE master_data_import_units
                SET payload = jsonb_set(payload, '{display_name}', to_jsonb(%s::text))
                WHERE school_id = %s AND batch_id = %s
                """,
                ("被篡改教师", import_actor_facts.school_id, staged.batch_id),
            )
        connection.commit()
    decisions = (
        (
            contracts.ImportUnitDecision(
                sequence=1,
                expected_unit_version=staged.units[0].unit_version,
                continue_reason="已核对为不同人员",
            ),
        )
        if staged.units[0].soft_anomaly_codes
        else ()
    )
    with pytest.raises(application.MasterDataImportVersionConflict):
        service.confirm(
            contracts.ConfirmImportBatchCommand(
                batch_id=staged.batch_id,
                expected_batch_version=staged.batch_version,
                confirmation_card_sha256=staged.confirmation_card_sha256,
                decisions=decisions,
            ),
            context=_command_context(
                import_actor_facts,
                idempotency_key=f"task8-confirm-card-integrity-{marker}",
            ),
        )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        formal_count = connection.execute(
            "SELECT count(*) FROM school_person_names WHERE school_id = %s AND source_ref = %s",
            (import_actor_facts.school_id, f"card-integrity-{marker}"),
        ).fetchone()
    assert formal_count == (0,)


@pytest.mark.integration
def _retired_legacy_test_ordinary_confirm_rechecks_same_name_created_after_preflight(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """预检后新出现同名时先失败该单元并刷新确认卡，不能沿用旧卡静默写入。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    valid_from = datetime.now(UTC).replace(microsecond=0)
    content = _workbook_bytes(("姓名", "编号"), ("竞态同名教师", marker), title="教师名单")
    staged = service.stage_ordinary(
        contracts.StageOrdinaryPersonImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"protected-race-same-name-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="teacher-roster.xlsx",
                size_bytes=len(content),
                worksheet_names=("教师名单",),
            ),
            worksheet_name="教师名单",
            mapping=contracts.ColumnMappingDraft(
                mapping_version=1,
                entries=(
                    contracts.ColumnMappingEntry(
                        source_column_index=1,
                        source_header="姓名",
                        target_field_code="person.display_name",
                    ),
                    contracts.ColumnMappingEntry(
                        source_column_index=2,
                        source_header="编号",
                        target_field_code="person.external_reference",
                    ),
                ),
            ),
            units=(
                contracts.PersonImportUnitDraft(
                    row_number=2,
                    display_name="竞态同名教师",
                    external_reference=marker,
                    valid_from=valid_from,
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-race-same-name-{marker}",
        ),
    )
    ready = service.preflight(
        contracts.PreflightImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            expected_mapping_version=1,
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-preflight-race-same-name-{marker}",
        ),
    )
    assert ready.units[0].soft_anomaly_codes == ()
    _seed_confirmed_person_name(
        current_master_data_schema,
        school_id=import_actor_facts.school_id,
        display_name="竞态同名教师",
        external_reference=f"existing-race-{marker}",
    )
    first = service.confirm(
        contracts.ConfirmImportBatchCommand(
            batch_id=ready.batch_id,
            expected_batch_version=ready.batch_version,
            confirmation_card_sha256=ready.confirmation_card_sha256,
            decisions=(
                contracts.ImportUnitDecision(
                    sequence=1,
                    expected_unit_version=ready.units[0].unit_version,
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-confirm-race-same-name-{marker}",
        ),
    )
    assert first.status == "partially_failed"
    assert first.failed_units == (1,)
    refreshed = service.preview(
        ready.batch_id,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-preview-race-same-name-{marker}",
        ),
    )
    assert refreshed.units[0].soft_anomaly_codes == ("same_display_name",)
    resumed = service.resume(
        contracts.ConfirmImportBatchCommand(
            batch_id=refreshed.batch_id,
            expected_batch_version=refreshed.batch_version,
            confirmation_card_sha256=refreshed.confirmation_card_sha256,
            decisions=(
                contracts.ImportUnitDecision(
                    sequence=1,
                    expected_unit_version=refreshed.units[0].unit_version,
                    continue_reason="已核对为不同人员",
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-resume-race-same-name-{marker}",
        ),
    )
    assert resumed.status == "applied"


@pytest.mark.integration
def _retired_legacy_test_same_name_is_soft_anomaly_and_never_auto_merged(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """同名只提示；没有人工理由不能继续，有理由后仍创建独立稳定人员 ID。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    existing_person_id = uuid4()
    valid_from = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "INSERT INTO school_people (id, school_id, status, version) "
            "VALUES (%s, %s, 'active', 1)",
            (existing_person_id, import_actor_facts.school_id),
        )
        connection.execute(
            """
            INSERT INTO school_person_names (
                id, school_id, person_id, display_name, valid_from,
                source_kind, source_ref, status, version
            ) VALUES (%s, %s, %s, '李明', %s,
                      'person_external_reference', %s, 'confirmed', 1)
            """,
            (
                uuid4(),
                import_actor_facts.school_id,
                existing_person_id,
                valid_from,
                f"existing-{marker}",
            ),
        )
        connection.commit()
    content = _workbook_bytes(("姓名", "编号"), ("李明", marker), title="教师名单")
    staged = service.stage_ordinary(
        contracts.StageOrdinaryPersonImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"protected-same-name-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="teacher-roster.xlsx",
                size_bytes=len(content),
                worksheet_names=("教师名单",),
            ),
            worksheet_name="教师名单",
            mapping=contracts.ColumnMappingDraft(
                mapping_version=1,
                entries=(
                    contracts.ColumnMappingEntry(
                        source_column_index=1,
                        source_header="姓名",
                        target_field_code="person.display_name",
                    ),
                    contracts.ColumnMappingEntry(
                        source_column_index=2,
                        source_header="编号",
                        target_field_code="person.external_reference",
                    ),
                ),
            ),
            units=(
                contracts.PersonImportUnitDraft(
                    row_number=2,
                    display_name="李明",
                    external_reference=marker,
                    valid_from=valid_from,
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-same-name-{marker}",
        ),
    )
    ready = service.preflight(
        contracts.PreflightImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            expected_mapping_version=1,
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-preflight-same-name-{marker}",
        ),
    )
    assert ready.units[0].soft_anomaly_codes == ("same_display_name",)
    without_reason = contracts.ConfirmImportBatchCommand(
        batch_id=ready.batch_id,
        expected_batch_version=ready.batch_version,
        confirmation_card_sha256=ready.confirmation_card_sha256,
        decisions=(
            contracts.ImportUnitDecision(
                sequence=1,
                expected_unit_version=ready.units[0].unit_version,
            ),
        ),
    )
    with pytest.raises(application.MasterDataImportStateConflict):
        service.confirm(
            without_reason,
            context=_command_context(
                import_actor_facts,
                idempotency_key=f"task8-confirm-same-name-without-reason-{marker}",
            ),
        )
    service.confirm(
        without_reason.model_copy(
            update={
                "decisions": (
                    without_reason.decisions[0].model_copy(
                        update={"continue_reason": "已核对为两名不同教师"}
                    ),
                )
            }
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-confirm-same-name-with-reason-{marker}",
        ),
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        people = connection.execute(
            """
            SELECT person_id FROM school_person_names
            WHERE school_id = %s AND display_name = '李明' AND status = 'confirmed'
            ORDER BY person_id
            """,
            (import_actor_facts.school_id,),
        ).fetchall()
    assert len(people) == 2
    assert len({row[0] for row in people}) == 2


@pytest.mark.integration
@pytest.mark.parametrize("failure_boundary", ("audit", "outbox"))
def _retired_legacy_test_manual_confirm_infrastructure_failure_restores_full_snapshot(
    failure_boundary: str,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """Manual confirmation keeps its entire staged write set on audit/Outbox failure."""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    service, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    staged = service.stage_manual(
        contracts.StageManualPersonUnitCommand(
            source_reference=f"manual-infrastructure-rollback-{marker}",
            unit=contracts.PersonImportUnitDraft(
                row_number=1,
                display_name="Manual rollback teacher",
                external_reference=f"manual-infrastructure-rollback-{marker}",
                valid_from=datetime.now(UTC).replace(microsecond=0),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-manual-infrastructure-{marker}",
        ),
    )
    confirm_context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-confirm-manual-infrastructure-{marker}",
    )
    facts = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=confirm_context,
        school_id=import_actor_facts.school_id,
        batch_id=staged.batch_id,
    )
    before = facts.snapshot()
    signature = (
        "app.append_master_data_import_audit(integer,uuid,uuid,uuid,bigint,text,text,"
        "text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)"
    )
    outbox_columns = (
        "id, school_id, event_key, event_type, aggregate_type, aggregate_id, "
        "aggregate_version, occurred_at, payload"
    )
    try:
        with current_master_data_schema.migration_connection() as connection:
            if failure_boundary == "audit":
                connection.execute("SET ROLE campus_ai_audit_definer")
                connection.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM campus_ai_runtime")
            else:
                connection.execute("SET ROLE campus_ai_db_owner")
                connection.execute(
                    f"REVOKE INSERT ({outbox_columns}) ON TABLE public.outbox_messages "
                    "FROM campus_ai_runtime"
                )
            connection.commit()
        with pytest.raises(ProgrammingError):
            service.confirm(
                contracts.ConfirmImportBatchCommand(
                    batch_id=staged.batch_id,
                    expected_batch_version=staged.batch_version,
                    confirmation_card_sha256=staged.confirmation_card_sha256,
                    decisions=(),
                ),
                context=confirm_context,
            )
    finally:
        with current_master_data_schema.migration_connection() as connection:
            if failure_boundary == "audit":
                connection.execute("SET ROLE campus_ai_audit_definer")
                connection.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_runtime")
            else:
                connection.execute("SET ROLE campus_ai_db_owner")
                connection.execute(
                    f"GRANT INSERT ({outbox_columns}) ON TABLE public.outbox_messages "
                    "TO campus_ai_runtime"
                )
            connection.commit()
    assert facts.snapshot() == before


@pytest.mark.integration
@pytest.mark.parametrize("failure_boundary", ("audit", "outbox"))
def _retired_legacy_test_calendar_publish_infrastructure_failure_restores_full_snapshot(
    failure_boundary: str,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """即使旧/新校历已在事务内变更，审计失败也必须让所有正式变化和 Outbox 一起回滚。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    service, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    old_calendar_id = uuid4()
    new_valid_from = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO school_calendar_versions (
                id, school_id, display_name, timezone_name,
                valid_from, source_kind, source_ref, status, version
            ) VALUES (%s, %s, '旧校历', 'Asia/Shanghai', %s,
                      'manual', %s, 'published', 1)
            """,
            (
                old_calendar_id,
                import_actor_facts.school_id,
                new_valid_from - timedelta(days=30),
                f"old-calendar-rollback-{marker}",
            ),
        )
        connection.commit()
    content = _workbook_bytes(("日期", "类型"), ("2026-09-02", "上课"), title="校历")
    staged = service.stage_authoritative_calendar(
        contracts.StageAuthoritativeCalendarImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"calendar-rollback-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="school-calendar.xlsx",
                size_bytes=len(content),
                worksheet_names=("校历",),
            ),
            draft=contracts.AuthoritativeCalendarDraft(
                display_name="不可半发布的新校历",
                timezone_name="Asia/Shanghai",
                valid_from=new_valid_from,
                valid_to=None,
                days=(
                    contracts.CalendarDayDraft(
                        local_date=date(2026, 9, 2),
                        day_kind="working",
                        actual_start_time=time(8, 0),
                        actual_end_time=time(17, 30),
                        note=None,
                    ),
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-calendar-rollback-{marker}",
        ),
    )
    signature = (
        "app.append_master_data_import_audit(integer,uuid,uuid,uuid,bigint,text,text,"
        "text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)"
    )
    outbox_columns = (
        "id, school_id, event_key, event_type, aggregate_type, aggregate_id, "
        "aggregate_version, occurred_at, payload"
    )
    publish_context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-publish-calendar-rollback-{marker}",
    )
    full_facts = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=publish_context,
        school_id=import_actor_facts.school_id,
        batch_id=staged.batch_id,
    )
    before = full_facts.snapshot()
    try:
        with current_master_data_schema.migration_connection() as connection:
            if failure_boundary == "audit":
                connection.execute("SET ROLE campus_ai_audit_definer")
                connection.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM campus_ai_runtime")
            else:
                connection.execute("SET ROLE campus_ai_db_owner")
                connection.execute(
                    f"REVOKE INSERT ({outbox_columns}) ON TABLE public.outbox_messages "
                    "FROM campus_ai_runtime"
                )
            connection.commit()
        with pytest.raises(ProgrammingError):
            service.publish_calendar(
                contracts.ConfirmImportBatchCommand(
                    batch_id=staged.batch_id,
                    expected_batch_version=staged.batch_version,
                    confirmation_card_sha256=staged.confirmation_card_sha256,
                    decisions=(),
                ),
                context=publish_context,
            )
    finally:
        with current_master_data_schema.migration_connection() as connection:
            if failure_boundary == "audit":
                connection.execute("SET ROLE campus_ai_audit_definer")
                connection.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_runtime")
            else:
                connection.execute("SET ROLE campus_ai_db_owner")
                connection.execute(
                    f"GRANT INSERT ({outbox_columns}) ON TABLE public.outbox_messages "
                    "TO campus_ai_runtime"
                )
            connection.commit()

    assert full_facts.snapshot() == before

    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        old_row = connection.execute(
            "SELECT status, valid_to, version FROM school_calendar_versions WHERE id = %s",
            (old_calendar_id,),
        ).fetchone()
        draft_row = connection.execute(
            """
            SELECT calendar.status, calendar.version, batch.status, unit.status
            FROM master_data_import_batches AS batch
            JOIN master_data_import_units AS unit
              ON unit.school_id = batch.school_id AND unit.batch_id = batch.id
            JOIN school_calendar_versions AS calendar
              ON calendar.school_id = unit.school_id AND calendar.id = unit.target_id
            WHERE batch.school_id = %s AND batch.id = %s
            """,
            (import_actor_facts.school_id, staged.batch_id),
        ).fetchone()
        side_effects = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM audit_logs
                 WHERE school_id = %s AND event_type = 'master_data.calendar.published'),
                (SELECT count(*) FROM outbox_messages
                 WHERE school_id = %s AND event_type = 'master_data.calendar.published')
            """,
            (import_actor_facts.school_id, import_actor_facts.school_id),
        ).fetchone()
    assert old_row == ("published", None, 1)
    assert draft_row == ("draft", 1, "ready_for_confirmation", "ready")
    assert side_effects == (0, 0)


@pytest.mark.integration
def _retired_legacy_test_calendar_audit_rejects_calendar_source_ref_not_bound_to_batch(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """Publishing may not audit a calendar whose protected source differs from its batch."""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    service, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    content = _workbook_bytes(("date", "kind"), ("2026-09-03", "working"), title="calendar")
    staged = service.stage_authoritative_calendar(
        contracts.StageAuthoritativeCalendarImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"calendar-source-bound-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="school-calendar.xlsx",
                size_bytes=len(content),
                worksheet_names=("calendar",),
            ),
            draft=contracts.AuthoritativeCalendarDraft(
                display_name="Source-bound calendar",
                timezone_name="Asia/Shanghai",
                valid_from=datetime.now(UTC).replace(microsecond=0) + timedelta(days=60),
                valid_to=None,
                days=(
                    contracts.CalendarDayDraft(
                        local_date=date.today() + timedelta(days=60),
                        day_kind="working",
                        actual_start_time=time(8, 0),
                        actual_end_time=time(17, 30),
                        note=None,
                    ),
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-calendar-source-bound-{marker}",
        ),
    )
    publish_context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-publish-calendar-source-bound-{marker}",
    )
    facts = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=publish_context,
        school_id=import_actor_facts.school_id,
        batch_id=staged.batch_id,
    )
    before = facts.snapshot()
    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        try:
            connection.execute("SET LOCAL ROLE campus_ai_runtime")
            set_local_execution_context(cast(Any, connection), _envelope_factory(publish_context))
            connection.execute("RESET ROLE")
            connection.execute("SET LOCAL ROLE campus_ai_db_owner")
            unit_row = connection.execute(
                "SELECT id, target_id FROM master_data_import_units "
                "WHERE school_id = %s AND batch_id = %s",
                (import_actor_facts.school_id, staged.batch_id),
            ).fetchone()
            assert unit_row is not None
            unit_id, calendar_id = unit_row
            record_id = uuid4()
            key_hash = sha256(publish_context.idempotency_key.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO command_idempotency_records (
                    id, school_id, action_code, actor_user_id, actor_person_id,
                    actor_context_type, actor_context_key, actor_assignment_id,
                    actor_assignment_version, resource_type, resource_id,
                    idempotency_key_hash, request_sha256, status, result_type,
                    result_payload
                ) VALUES (
                    %s, %s, 'master_data.import.calendar_publish', %s, %s,
                    'assignment', %s, %s, 1, 'master_data.import_batch', %s,
                    %s, %s, 'succeeded', 'master_data.import_outcome',
                    jsonb_build_object(
                        'resource_type', 'master_data.import_batch',
                        'resource_id', %s::text, 'resource_version', 2,
                        'outcome_code', 'published'
                    )
                )
                """,
                (
                    record_id,
                    import_actor_facts.school_id,
                    import_actor_facts.actor_user_id,
                    import_actor_facts.actor_person_id,
                    f"assignment:{import_actor_facts.actor_assignment_id}:1",
                    import_actor_facts.actor_assignment_id,
                    str(staged.batch_id),
                    key_hash,
                    "f" * 64,
                    str(staged.batch_id),
                ),
            )
            connection.execute(
                "UPDATE school_calendar_versions SET status = 'published', version = 2, "
                "source_ref = %s WHERE school_id = %s AND id = %s",
                (f"unbound-calendar-source-{marker}", import_actor_facts.school_id, calendar_id),
            )
            connection.execute(
                "UPDATE master_data_import_units SET status = 'published', "
                "target_version = 2, version = version + 1 "
                "WHERE school_id = %s AND id = %s",
                (import_actor_facts.school_id, unit_id),
            )
            connection.execute(
                "UPDATE master_data_import_batches SET status = 'published', "
                "succeeded_units = 1, pending_units = 0, version = 2, "
                "confirmed_by_user_id = %s, confirmed_by_person_id = %s, "
                "confirmed_by_assignment_id = %s, confirmed_at = clock_timestamp() "
                "WHERE school_id = %s AND id = %s",
                (
                    import_actor_facts.actor_user_id,
                    import_actor_facts.actor_person_id,
                    import_actor_facts.actor_assignment_id,
                    import_actor_facts.school_id,
                    staged.batch_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO master_data_import_command_outcomes (
                    id, school_id, idempotency_record_id, batch_id, action_code,
                    status, batch_version, succeeded_units, failed_units, executed_units
                ) VALUES (
                    %s, %s, %s, %s, 'master_data.import.calendar_publish',
                    'published', 2, ARRAY[1], ARRAY[]::integer[], ARRAY[1]
                )
                """,
                (uuid4(), import_actor_facts.school_id, record_id, staged.batch_id),
            )
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="master data import audit fact mismatch",
            ):
                connection.execute(
                    """
                    SELECT app.append_master_data_import_audit(
                        %s, %s, %s, %s, 1, %s,
                        'master_data.calendar.published', 'school_calendar_version',
                        %s, %s, %s,
                        jsonb_build_object(
                            'state_code', 'calendar.draft', 'resource_version', 1,
                            'stable_reference_ids', jsonb_build_array(%s::text),
                            'fact_codes', '[]'::jsonb
                        ),
                        jsonb_build_object(
                            'state_code', 'calendar.published', 'resource_version', 2,
                            'stable_reference_ids', jsonb_build_array(%s::text),
                            'fact_codes', '["calendar.published"]'::jsonb
                        ),
                        %s, 'excel.calendar-published'
                    )
                    """,
                    (
                        import_actor_facts.actor_user_id,
                        import_actor_facts.school_id,
                        import_actor_facts.actor_person_id,
                        import_actor_facts.actor_assignment_id,
                        key_hash,
                        calendar_id,
                        staged.batch_id,
                        unit_id,
                        calendar_id,
                        calendar_id,
                        publish_context.request_id,
                    ),
                )
        finally:
            connection.rollback()
    assert facts.snapshot() == before


@pytest.mark.integration
def _retired_legacy_test_existing_external_reference_is_hard_preflight_error_with_safe_field_report(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """稳定外部编号冲突必须在预检阻断，并只保存行号、错误码和中文字段名。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    existing_person_id = uuid4()
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "INSERT INTO school_people (id, school_id, status, version) "
            "VALUES (%s, %s, 'active', 1)",
            (existing_person_id, import_actor_facts.school_id),
        )
        connection.execute(
            """
            INSERT INTO school_person_names (
                id, school_id, person_id, display_name, valid_from,
                source_kind, source_ref, status, version
            ) VALUES (%s, %s, %s, '已有教师', %s,
                      'person_external_reference', %s, 'confirmed', 1)
            """,
            (
                uuid4(),
                import_actor_facts.school_id,
                existing_person_id,
                datetime.now(UTC).replace(microsecond=0),
                marker,
            ),
        )
        connection.commit()
    content = _workbook_bytes(("姓名", "编号"), ("另一教师", marker), title="教师名单")
    staged = service.stage_ordinary(
        contracts.StageOrdinaryPersonImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"protected-conflict-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="teacher-roster.xlsx",
                size_bytes=len(content),
                worksheet_names=("教师名单",),
            ),
            worksheet_name="教师名单",
            mapping=contracts.ColumnMappingDraft(
                mapping_version=1,
                entries=(
                    contracts.ColumnMappingEntry(
                        source_column_index=1,
                        source_header="姓名",
                        target_field_code="person.display_name",
                    ),
                    contracts.ColumnMappingEntry(
                        source_column_index=2,
                        source_header="编号",
                        target_field_code="person.external_reference",
                    ),
                ),
            ),
            units=(
                contracts.PersonImportUnitDraft(
                    row_number=2,
                    display_name="另一教师",
                    external_reference=marker,
                    valid_from=datetime.now(UTC).replace(microsecond=0),
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-conflict-{marker}",
        ),
    )
    failed = service.preflight(
        contracts.PreflightImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            expected_mapping_version=1,
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-preflight-conflict-{marker}",
        ),
    )
    assert failed.status == "preflight_failed"
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        error_row = connection.execute(
            """
            SELECT row_number, error_code, error_field_code
            FROM master_data_import_units
            WHERE school_id = %s AND batch_id = %s
            """,
            (import_actor_facts.school_id, staged.batch_id),
        ).fetchone()
        formal_count = connection.execute(
            """
            SELECT count(*) FROM school_person_names
            WHERE school_id = %s AND source_kind = 'person_external_reference'
              AND source_ref = %s
            """,
            (import_actor_facts.school_id, marker),
        ).fetchone()
    assert error_row == (2, "external_reference_conflict", "校内编号")
    assert formal_count == (1,)


@pytest.mark.integration
def _retired_legacy_test_resume_recovers_pending_unit_after_process_stops_in_applying_state(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """进程在批次进入 applying 后中断时，续跑必须接管 pending 单元而不是制造僵尸批次。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    application = importlib.import_module("app.platform.master_data_imports.application")
    service = application.MasterDataImportService(
        uow_factory=application.SqlAlchemyMasterDataImportUoWFactory(
            session_factory=import_runtime_session_factory,
            envelope_factory=_envelope_factory,
            lock_timeout_ms=750,
        ),
        authorizer=application.ConfiguredMasterDataImportAuthorizer(
            frozenset({"school_master_data_manager"})
        ),
    )
    marker = uuid4().hex
    content = _workbook_bytes(("姓名", "编号"), ("测试教师", marker), title="教师名单")
    staged = service.stage_ordinary(
        contracts.StageOrdinaryPersonImportCommand(
            source=contracts.ProtectedImportSourceRef(
                opaque_ref=f"protected-recovery-{marker}",
                source_sha256=sha256(content).hexdigest(),
                filename_summary="teacher-roster.xlsx",
                size_bytes=len(content),
                worksheet_names=("教师名单",),
            ),
            worksheet_name="教师名单",
            mapping=contracts.ColumnMappingDraft(
                mapping_version=1,
                entries=(
                    contracts.ColumnMappingEntry(
                        source_column_index=1,
                        source_header="姓名",
                        target_field_code="person.display_name",
                    ),
                    contracts.ColumnMappingEntry(
                        source_column_index=2,
                        source_header="编号",
                        target_field_code="person.external_reference",
                    ),
                ),
            ),
            units=(
                contracts.PersonImportUnitDraft(
                    row_number=2,
                    display_name="测试教师",
                    external_reference=marker,
                    valid_from=datetime.now(UTC).replace(microsecond=0),
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-recovery-{marker}",
        ),
    )
    ready = service.preflight(
        contracts.PreflightImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            expected_mapping_version=1,
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-preflight-recovery-{marker}",
        ),
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("BEGIN")
        connection.execute("SET ROLE campus_ai_db_owner")
        for name, value in (
            ("app.context_purpose", "command"),
            ("app.context_kind", "assignment"),
            ("app.account_user_id", str(import_actor_facts.actor_user_id)),
            ("app.person_id", str(import_actor_facts.actor_person_id)),
            ("app.assignment_id", str(import_actor_facts.actor_assignment_id)),
            ("app.assignment_version", "1"),
        ):
            connection.execute("SELECT set_config(%s, %s, true)", (name, value))
        connection.execute(
            """
            UPDATE master_data_import_batches
            SET status = 'applying', version = version + 1,
                confirmed_by_user_id = %s, confirmed_by_person_id = %s,
                confirmed_by_assignment_id = %s, confirmed_at = clock_timestamp()
            WHERE school_id = %s AND id = %s
            """,
            (
                import_actor_facts.actor_user_id,
                import_actor_facts.actor_person_id,
                import_actor_facts.actor_assignment_id,
                import_actor_facts.school_id,
                ready.batch_id,
            ),
        )
        connection.commit()
    interrupted = service.preview(
        ready.batch_id,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-preview-recovery-{marker}",
        ),
    )
    recovered = service.resume(
        contracts.ConfirmImportBatchCommand(
            batch_id=interrupted.batch_id,
            expected_batch_version=interrupted.batch_version,
            confirmation_card_sha256=interrupted.confirmation_card_sha256,
            decisions=(
                contracts.ImportUnitDecision(
                    sequence=1,
                    expected_unit_version=interrupted.units[0].unit_version,
                ),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-resume-recovery-{marker}",
        ),
    )
    assert recovered.status == "applied"
    assert recovered.executed_units == (1,)


@pytest.mark.integration
@pytest.mark.parametrize("failure_boundary", ("audit", "outbox"))
def _retired_legacy_test_ordinary_unit_infrastructure_failure_rolls_back_and_propagates(
    failure_boundary: str,
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
) -> None:
    """单元内 audit/Outbox 基础设施异常向外传播并完整回滚单元事务。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    service, uow_factory = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    content = _workbook_bytes(("姓名", "编号"), ("测试教师", marker), title="教师名单")
    excel = importlib.import_module("app.platform.master_data_imports.excel")
    staged_command = excel.RestrictedExcelPersonMapper().map(
        BytesIO(content),
        source=contracts.ProtectedImportSourceRef(
            opaque_ref=f"protected-unit-rollback-{marker}",
            source_sha256=sha256(content).hexdigest(),
            filename_summary="teacher-roster.xlsx",
            size_bytes=len(content),
            worksheet_names=("教师名单",),
        ),
        worksheet_name="教师名单",
        mapping=contracts.ColumnMappingDraft(
            mapping_version=1,
            entries=(
                contracts.ColumnMappingEntry(
                    source_column_index=1,
                    source_header="姓名",
                    target_field_code="person.display_name",
                ),
                contracts.ColumnMappingEntry(
                    source_column_index=2,
                    source_header="编号",
                    target_field_code="person.external_reference",
                ),
            ),
        ),
        default_valid_from=datetime.now(UTC).replace(microsecond=0),
    )
    staged = service.stage_ordinary(
        staged_command,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-stage-unit-rollback-{marker}",
        ),
    )
    ready = service.preflight(
        contracts.PreflightImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            expected_mapping_version=1,
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-preflight-unit-rollback-{marker}",
        ),
    )
    unit_boundary_snapshots: list[tuple[Any, ...]] = []
    original_apply = service._apply_ordinary_person_unit

    def capture_full_snapshot_before_unit(**kwargs: Any) -> bool:
        facts = TerminalImportFacts(
            roles=current_master_data_schema,
            uow_factory=uow_factory,
            context=kwargs["context"],
            school_id=import_actor_facts.school_id,
            batch_id=ready.batch_id,
        )
        unit_boundary_snapshots.append(facts.snapshot())
        return original_apply(**kwargs)

    monkeypatch.setattr(service, "_apply_ordinary_person_unit", capture_full_snapshot_before_unit)
    signature = (
        "app.append_master_data_import_audit(integer,uuid,uuid,uuid,bigint,text,text,"
        "text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)"
    )
    outbox_columns = (
        "id, school_id, event_key, event_type, aggregate_type, aggregate_id, "
        "aggregate_version, occurred_at, payload"
    )
    try:
        with current_master_data_schema.migration_connection() as connection:
            if failure_boundary == "audit":
                connection.execute("SET ROLE campus_ai_audit_definer")
                connection.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM campus_ai_runtime")
            else:
                connection.execute("SET ROLE campus_ai_db_owner")
                connection.execute(
                    f"REVOKE INSERT ({outbox_columns}) ON TABLE public.outbox_messages "
                    "FROM campus_ai_runtime"
                )
            connection.commit()
        with pytest.raises(ProgrammingError):
            service.confirm(
                contracts.ConfirmImportBatchCommand(
                    batch_id=ready.batch_id,
                    expected_batch_version=ready.batch_version,
                    confirmation_card_sha256=ready.confirmation_card_sha256,
                    decisions=(
                        contracts.ImportUnitDecision(
                            sequence=1,
                            expected_unit_version=ready.units[0].unit_version,
                        ),
                    ),
                ),
                context=_command_context(
                    import_actor_facts,
                    idempotency_key=f"task8-confirm-unit-rollback-{marker}",
                ),
            )
    finally:
        with current_master_data_schema.migration_connection() as connection:
            if failure_boundary == "audit":
                connection.execute("SET ROLE campus_ai_audit_definer")
                connection.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_runtime")
            else:
                connection.execute("SET ROLE campus_ai_db_owner")
                connection.execute(
                    f"GRANT INSERT ({outbox_columns}) ON TABLE public.outbox_messages "
                    "TO campus_ai_runtime"
                )
            connection.commit()
    assert len(unit_boundary_snapshots) == 1
    after_failure = TerminalImportFacts(
        roles=current_master_data_schema,
        uow_factory=uow_factory,
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"task8-confirm-unit-rollback-{marker}",
        ),
        school_id=import_actor_facts.school_id,
        batch_id=ready.batch_id,
    ).snapshot()
    assert after_failure == unit_boundary_snapshots[0]
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        rollback_facts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM school_person_names
                 WHERE school_id = %s AND source_ref = %s),
                (SELECT count(*) FROM audit_logs
                 WHERE school_id = %s AND event_type = 'master_data.person.imported'),
                (SELECT count(*) FROM outbox_messages
                 WHERE school_id = %s AND event_type = 'master_data.person.imported'),
                (SELECT count(*) FROM master_data_import_command_outcomes
                 WHERE school_id = %s AND batch_id = %s),
                (SELECT row(status, target_id, target_version, error_code,
                            last_attempt_completed)
                 FROM master_data_import_units
                 WHERE school_id = %s AND batch_id = %s AND sequence = 1),
                (SELECT row(status, succeeded_units, failed_units, pending_units)
                 FROM master_data_import_batches
                 WHERE school_id = %s AND id = %s),
                (SELECT row(record.status, record.result_type, record.result_payload)
                 FROM command_idempotency_records AS record
                 JOIN master_data_import_batches AS batch
                   ON batch.school_id = record.school_id
                  AND batch.active_idempotency_record_id = record.id
                 WHERE batch.school_id = %s AND batch.id = %s)
            """,
            (
                import_actor_facts.school_id,
                marker,
                import_actor_facts.school_id,
                import_actor_facts.school_id,
                import_actor_facts.school_id,
                ready.batch_id,
                import_actor_facts.school_id,
                ready.batch_id,
                import_actor_facts.school_id,
                ready.batch_id,
                import_actor_facts.school_id,
                ready.batch_id,
            ),
        ).fetchone()
    assert rollback_facts == (
        0,
        0,
        0,
        0,
        ("ready", None, None, None, "f"),
        ("applying", "0", "0", "1"),
        ("in_progress", None, None),
    )


@pytest.mark.integration
def _retired_legacy_test_stage_manual_unauthorized_assignment_is_zero_write(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    other_import_actor_facts: ImportActorFacts,
) -> None:
    """未授权任职在登记手工批次前拒绝，两个学校的完整写集合均不变化。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    command = contracts.StageManualPersonUnitCommand(
        source_reference=f"zero-write-manual-{marker}",
        unit=contracts.PersonImportUnitDraft(
            row_number=1,
            display_name="零写入测试教师",
            external_reference=f"zero-write-{marker}",
            valid_from=datetime.now(UTC).replace(microsecond=0),
        ),
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        changed = connection.execute(
            """
            UPDATE staff_assignments
            SET semantic_role_code = 'teacher'
            WHERE school_id = %s AND id = %s
            """,
            (import_actor_facts.school_id, import_actor_facts.actor_assignment_id),
        ).rowcount
        content_changed = connection.execute(
            """
            UPDATE staff_assignment_versions
            SET semantic_role_code = 'teacher'
            WHERE school_id = %s AND assignment_id = %s
            """,
            (import_actor_facts.school_id, import_actor_facts.actor_assignment_id),
        ).rowcount
    assert changed == 1
    assert content_changed >= 1
    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-zero-write-manual-{marker}",
    )
    before = (
        _full_import_write_snapshot(current_master_data_schema, import_actor_facts.school_id),
        _full_import_write_snapshot(current_master_data_schema, other_import_actor_facts.school_id),
    )

    with pytest.raises(application.MasterDataImportPermissionDenied):
        service.stage_manual(command, context=context)

    after = (
        _full_import_write_snapshot(current_master_data_schema, import_actor_facts.school_id),
        _full_import_write_snapshot(current_master_data_schema, other_import_actor_facts.school_id),
    )
    assert after == before


def _zero_write_calendar_stage_command(marker: str) -> Any:
    """构造只含合成事实的权威校历草稿命令。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    return contracts.StageAuthoritativeCalendarImportCommand(
        source=contracts.ProtectedImportSourceRef(
            opaque_ref=f"protected-zero-write-calendar-{marker}",
            source_sha256=sha256(marker.encode("utf-8")).hexdigest(),
            filename_summary="zero-write-calendar.xlsx",
            size_bytes=1024,
            worksheet_names=("校历",),
        ),
        draft=contracts.AuthoritativeCalendarDraft(
            display_name="零写入测试校历",
            timezone_name="Asia/Shanghai",
            valid_from=datetime.now(UTC).replace(microsecond=0) + timedelta(days=30),
            valid_to=None,
            days=(
                contracts.CalendarDayDraft(
                    local_date=date.today() + timedelta(days=30),
                    day_kind="working",
                    actual_start_time=time(8, 0),
                    actual_end_time=time(17, 0),
                    note="合成校历事实",
                ),
            ),
        ),
    )


def _zero_write_ordinary_stage_command(marker: str) -> Any:
    """构造单单元普通导入草稿命令。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    return contracts.StageOrdinaryPersonImportCommand(
        source=contracts.ProtectedImportSourceRef(
            opaque_ref=f"protected-zero-write-ordinary-{marker}",
            source_sha256=sha256(marker.encode("utf-8")).hexdigest(),
            filename_summary="zero-write-roster.xlsx",
            size_bytes=1024,
            worksheet_names=("教师名单",),
        ),
        worksheet_name="教师名单",
        mapping=contracts.ColumnMappingDraft(
            mapping_version=1,
            entries=(
                contracts.ColumnMappingEntry(
                    source_column_index=1,
                    source_header="姓名",
                    target_field_code="person.display_name",
                ),
                contracts.ColumnMappingEntry(
                    source_column_index=2,
                    source_header="编号",
                    target_field_code="person.external_reference",
                ),
            ),
        ),
        units=(
            contracts.PersonImportUnitDraft(
                row_number=2,
                display_name="零写入普通导入教师",
                external_reference=f"zero-write-ordinary-{marker}",
                valid_from=datetime.now(UTC).replace(microsecond=0),
            ),
        ),
    )


def _prepare_zero_write_operation(
    entry: str,
    *,
    service: Any,
    actor: ImportActorFacts,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """完成场景种子后返回只接收可信上下文的目标写入口。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    marker = uuid4().hex
    if entry == "stage_manual":
        command = contracts.StageManualPersonUnitCommand(
            source_reference=f"zero-write-manual-matrix-{marker}",
            unit=contracts.PersonImportUnitDraft(
                row_number=1,
                display_name="零写入手工教师",
                external_reference=f"zero-write-manual-matrix-{marker}",
                valid_from=datetime.now(UTC).replace(microsecond=0),
            ),
        )
        return lambda context: service.stage_manual(command, context=context)
    if entry == "stage_ordinary":
        command = _zero_write_ordinary_stage_command(marker)
        return lambda context: service.stage_ordinary(command, context=context)
    if entry == "stage_authoritative_calendar":
        command = _zero_write_calendar_stage_command(marker)
        return lambda context: service.stage_authoritative_calendar(command, context=context)
    if entry == "preflight":
        staged = service.stage_ordinary(
            _zero_write_ordinary_stage_command(marker),
            context=_command_context(actor, idempotency_key=f"zero-write-stage-{marker}"),
        )
        command = contracts.PreflightImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            expected_mapping_version=1,
        )
        return lambda context: service.preflight(command, context=context)
    if entry == "confirm":
        staged = service.stage_manual(
            contracts.StageManualPersonUnitCommand(
                source_reference=f"zero-write-confirm-{marker}",
                unit=contracts.PersonImportUnitDraft(
                    row_number=1,
                    display_name="零写入确认教师",
                    external_reference=f"zero-write-confirm-{marker}",
                    valid_from=datetime.now(UTC).replace(microsecond=0),
                ),
            ),
            context=_command_context(actor, idempotency_key=f"zero-write-confirm-stage-{marker}"),
        )
        command = contracts.ConfirmImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            confirmation_card_sha256=staged.confirmation_card_sha256,
            decisions=(),
        )
        return lambda context: service.confirm(command, context=context)
    if entry == "publish_calendar":
        staged = service.stage_authoritative_calendar(
            _zero_write_calendar_stage_command(marker),
            context=_command_context(actor, idempotency_key=f"zero-write-calendar-stage-{marker}"),
        )
        command = contracts.ConfirmImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            confirmation_card_sha256=staged.confirmation_card_sha256,
            decisions=(),
        )
        return lambda context: service.publish_calendar(command, context=context)
    if entry == "resume":
        command, first_context = _ready_single_ordinary_confirmation(
            service,
            actor,
            marker=f"zero-write-resume-{marker}",
        )
        original_apply = service._apply_ordinary_person_unit

        def stop_before_first_unit(**_kwargs: Any) -> bool:
            raise _StopAfterActiveCommandCommit("活动命令已提交，尚未执行首个单元")

        monkeypatch.setattr(service, "_apply_ordinary_person_unit", stop_before_first_unit)
        with pytest.raises(_StopAfterActiveCommandCommit):
            service.confirm(command, context=first_context)
        monkeypatch.setattr(service, "_apply_ordinary_person_unit", original_apply)
        preview = service.preview(
            command.batch_id,
            context=_command_context(actor, idempotency_key=f"zero-write-resume-preview-{marker}"),
        )
        resume_command = contracts.ConfirmImportBatchCommand(
            batch_id=preview.batch_id,
            expected_batch_version=preview.batch_version,
            confirmation_card_sha256=preview.confirmation_card_sha256,
            decisions=tuple(
                contracts.ImportUnitDecision(
                    sequence=unit.sequence,
                    expected_unit_version=unit.unit_version,
                )
                for unit in preview.units
            ),
        )
        return lambda context: service.resume(resume_command, context=context)
    raise AssertionError(f"未知零写入入口：{entry}")


def _zero_write_rejected_context(
    violation: str,
    *,
    roles: PostgresRoles,
    actor: ImportActorFacts,
) -> tuple[CommandExecutionContext, type[BaseException]]:
    """在 snapshot 前形成单一 actor/assignment 失效事实。"""

    key = f"task8-zero-write-{violation}-{uuid4().hex}"
    if violation == "guardian":
        guardian = _issue_guardian_actor_context(
            account_user_id=actor.actor_user_id,
            school_id=SchoolId(actor.school_id),
            person_id=PersonId(actor.actor_person_id),
            guardian_relationship_id=GuardianRelationshipId(uuid4()),
            relationship_version=1,
            authorization_version=1,
        )
        return (
            compose_command_context(guardian, uuid4(), key),
            application.MasterDataImportContextRequired,
        )
    if violation == "person_mismatch":
        other = _seed_same_school_import_actor(roles, school_id=actor.school_id)
        mismatched = _issue_staff_actor_context(
            account_user_id=actor.actor_user_id,
            school_id=SchoolId(actor.school_id),
            person_id=PersonId(other.actor_person_id),
            assignment_id=AssignmentId(actor.actor_assignment_id),
            assignment_version=1,
            authorization_version=1,
        )
        return (
            compose_command_context(mismatched, uuid4(), key),
            application.MasterDataImportContextRequired,
        )
    assignments = {
        "unauthorized": (
            sql.SQL("semantic_role_code = 'teacher'"),
            sql.SQL("semantic_role_code = 'teacher'"),
        ),
        "revoked": (
            sql.SQL("status = 'rejected'"),
            sql.SQL("revoked_at = clock_timestamp()"),
        ),
        "expired": (
            sql.SQL("valid_to = clock_timestamp() - interval '1 second'"),
            sql.SQL("planned_valid_to = clock_timestamp() - interval '1 second'"),
        ),
        "not_yet_effective": (
            sql.SQL("valid_from = clock_timestamp() + interval '1 day'"),
            sql.SQL("effective_from = clock_timestamp() + interval '1 day'"),
        ),
        "stale_assignment": (sql.SQL("version = version + 1"), None),
    }
    stable_mutation, content_mutation = assignments[violation]
    with roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        changed = connection.execute(
            sql.SQL("UPDATE staff_assignments SET {} WHERE school_id = %s AND id = %s").format(
                stable_mutation
            ),
            (actor.school_id, actor.actor_assignment_id),
        ).rowcount
        content_changed = 0
        if content_mutation is not None:
            content_changed = connection.execute(
                sql.SQL(
                    "UPDATE staff_assignment_versions SET {} "
                    "WHERE school_id = %s AND assignment_id = %s"
                ).format(content_mutation),
                (actor.school_id, actor.actor_assignment_id),
            ).rowcount
    assert changed == 1
    if content_mutation is not None:
        assert content_changed >= 1
    expected = (
        application.MasterDataImportPermissionDenied
        if violation == "unauthorized"
        else application.MasterDataImportContextRequired
    )
    return _command_context(actor, idempotency_key=key), expected


@pytest.mark.integration
@pytest.mark.parametrize(
    "entry",
    [
        "stage_manual",
        "stage_ordinary",
        "stage_authoritative_calendar",
        "preflight",
        "confirm",
        "publish_calendar",
        "resume",
    ],
)
@pytest.mark.parametrize(
    "violation",
    [
        "guardian",
        "unauthorized",
        "revoked",
        "expired",
        "not_yet_effective",
        "stale_assignment",
        "person_mismatch",
    ],
)
def _retired_legacy_test_import_entry_actor_assignment_rejection_is_zero_write(
    entry: str,
    violation: str,
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    other_import_actor_facts: ImportActorFacts,
) -> None:
    """七个导入入口在任职拒绝后不改变本校或他校任何导入写事实。"""

    service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    operation = _prepare_zero_write_operation(
        entry,
        service=service,
        actor=import_actor_facts,
        monkeypatch=monkeypatch,
    )
    context, expected_error = _zero_write_rejected_context(
        violation,
        roles=current_master_data_schema,
        actor=import_actor_facts,
    )
    before = (
        _full_import_write_snapshot(current_master_data_schema, import_actor_facts.school_id),
        _full_import_write_snapshot(current_master_data_schema, other_import_actor_facts.school_id),
    )

    with pytest.raises(expected_error):
        operation(context)

    after = (
        _full_import_write_snapshot(current_master_data_schema, import_actor_facts.school_id),
        _full_import_write_snapshot(current_master_data_schema, other_import_actor_facts.school_id),
    )
    assert after == before


@pytest.mark.integration
@pytest.mark.parametrize(
    "violation",
    [
        "guardian",
        "unauthorized",
        "revoked",
        "expired",
        "not_yet_effective",
        "stale_assignment",
        "person_mismatch",
    ],
)
def _retired_legacy_test_take_over_actor_assignment_rejection_is_zero_write(
    violation: str,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """接管入口同样先拒绝无效新任职，并保持两个学校的完整写集合不变。"""

    scenario = applying_import_scenario
    scenario.revoke_original_assignment()
    replacement_actor = scenario.replacement_context.actor
    assert type(replacement_actor) is StaffActorContext
    facts = ImportActorFacts(
        school_id=scenario.school_id,
        actor_user_id=replacement_actor.account_user_id,
        actor_person_id=replacement_actor.person_id,
        actor_assignment_id=replacement_actor.assignment_id,
    )
    context, expected_error = _zero_write_rejected_context(
        violation,
        roles=scenario.roles,
        actor=facts,
    )
    before = (
        _full_import_write_snapshot(scenario.roles, scenario.school_id),
        _full_import_write_snapshot(
            scenario.roles,
            scenario.cross_school_id,
        ),
    )

    with pytest.raises(expected_error):
        scenario.replacement_service.take_over(scenario.takeover_command, context=context)

    after = (
        _full_import_write_snapshot(scenario.roles, scenario.school_id),
        _full_import_write_snapshot(
            scenario.roles,
            scenario.cross_school_id,
        ),
    )
    assert after == before


@pytest.mark.integration
@pytest.mark.parametrize(
    "entry",
    ["stage_manual", "stage_ordinary", "stage_authoritative_calendar"],
)
def _retired_legacy_test_stage_same_idempotency_key_changed_content_is_zero_write(
    entry: str,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    other_import_actor_facts: ImportActorFacts,
) -> None:
    """三类登记入口拒绝同键异内容，且拒绝本身不改变任一学校。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    if entry == "stage_manual":
        command = contracts.StageManualPersonUnitCommand(
            source_reference=f"zero-write-same-key-manual-{marker}",
            unit=contracts.PersonImportUnitDraft(
                row_number=1,
                display_name="幂等冲突教师甲",
                external_reference=f"zero-write-same-key-{marker}",
                valid_from=datetime.now(UTC).replace(microsecond=0),
            ),
        )
        changed = command.model_copy(
            update={"unit": command.unit.model_copy(update={"display_name": "幂等冲突教师乙"})}
        )
        operation = service.stage_manual
    elif entry == "stage_ordinary":
        command = _zero_write_ordinary_stage_command(marker)
        changed_unit = command.units[0].model_copy(update={"display_name": "幂等冲突普通教师"})
        changed = command.model_copy(update={"units": (changed_unit,)})
        operation = service.stage_ordinary
    else:
        command = _zero_write_calendar_stage_command(marker)
        changed = command.model_copy(
            update={"draft": command.draft.model_copy(update={"display_name": "幂等冲突变更校历"})}
        )
        operation = service.stage_authoritative_calendar
    idempotency_key = f"task8-zero-write-stage-same-key-{entry}-{marker}"
    operation(
        command,
        context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
    )
    before = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )

    with pytest.raises(application.MasterDataImportIdempotencyConflict):
        operation(
            changed,
            context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
        )

    after = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )
    assert after == before


def _prepare_cross_school_batch_operation(
    entry: str,
    *,
    service: Any,
    owner: ImportActorFacts,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """在目标学校完成种子后，返回将由另一学校上下文调用的写操作。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    marker = uuid4().hex
    if entry == "preflight":
        staged = service.stage_ordinary(
            _zero_write_ordinary_stage_command(marker),
            context=_command_context(owner, idempotency_key=f"zero-write-cross-stage-{marker}"),
        )
        command = contracts.PreflightImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            expected_mapping_version=1,
        )
        return lambda context: service.preflight(command, context=context)
    if entry == "confirm":
        staged = service.stage_manual(
            contracts.StageManualPersonUnitCommand(
                source_reference=f"zero-write-cross-confirm-{marker}",
                unit=contracts.PersonImportUnitDraft(
                    row_number=1,
                    display_name="跨校拒绝手工教师",
                    external_reference=f"zero-write-cross-confirm-{marker}",
                    valid_from=datetime.now(UTC).replace(microsecond=0),
                ),
            ),
            context=_command_context(
                owner,
                idempotency_key=f"zero-write-cross-confirm-stage-{marker}",
            ),
        )
        command = contracts.ConfirmImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            confirmation_card_sha256=staged.confirmation_card_sha256,
            decisions=(),
        )
        return lambda context: service.confirm(command, context=context)
    if entry == "publish_calendar":
        staged = service.stage_authoritative_calendar(
            _zero_write_calendar_stage_command(marker),
            context=_command_context(
                owner,
                idempotency_key=f"zero-write-cross-calendar-stage-{marker}",
            ),
        )
        command = contracts.ConfirmImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            confirmation_card_sha256=staged.confirmation_card_sha256,
            decisions=(),
        )
        return lambda context: service.publish_calendar(command, context=context)
    if entry == "resume":
        command, first_context = _ready_single_ordinary_confirmation(
            service,
            owner,
            marker=f"zero-write-cross-resume-{marker}",
        )
        original_apply = service._apply_ordinary_person_unit

        def stop_before_first_unit(**_kwargs: Any) -> bool:
            raise _StopAfterActiveCommandCommit("活动命令已提交，尚未执行首个单元")

        monkeypatch.setattr(service, "_apply_ordinary_person_unit", stop_before_first_unit)
        with pytest.raises(_StopAfterActiveCommandCommit):
            service.confirm(command, context=first_context)
        monkeypatch.setattr(service, "_apply_ordinary_person_unit", original_apply)
        preview = service.preview(
            command.batch_id,
            context=_command_context(
                owner,
                idempotency_key=f"zero-write-cross-resume-preview-{marker}",
            ),
        )
        resume_command = contracts.ConfirmImportBatchCommand(
            batch_id=preview.batch_id,
            expected_batch_version=preview.batch_version,
            confirmation_card_sha256=preview.confirmation_card_sha256,
            decisions=tuple(
                contracts.ImportUnitDecision(
                    sequence=unit.sequence,
                    expected_unit_version=unit.unit_version,
                )
                for unit in preview.units
            ),
        )
        return lambda context: service.resume(resume_command, context=context)
    raise AssertionError(f"未知跨校入口：{entry}")


@pytest.mark.integration
@pytest.mark.parametrize("entry", ["preflight", "confirm", "publish_calendar", "resume"])
def _retired_legacy_test_cross_school_batch_and_units_are_zero_write(
    entry: str,
    monkeypatch: pytest.MonkeyPatch,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    other_import_actor_facts: ImportActorFacts,
) -> None:
    """本校写入口看不见他校批次，其内全部单元也保持逐字段不变。"""

    service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    operation = _prepare_cross_school_batch_operation(
        entry,
        service=service,
        owner=other_import_actor_facts,
        monkeypatch=monkeypatch,
    )
    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-zero-write-cross-school-{entry}-{uuid4().hex}",
    )
    before = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )

    with pytest.raises(application.MasterDataImportStateConflict):
        operation(context)

    after = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )
    assert after == before


@pytest.mark.integration
def _retired_legacy_test_take_over_cross_school_batch_is_zero_write(
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """接管不能用本校新任职触碰另一学校批次。"""

    scenario = applying_import_scenario
    scenario.revoke_original_assignment()
    command = scenario.command_with_mutation("cross_school_batch")
    before = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )

    with pytest.raises(application.MasterDataImportStateConflict):
        scenario.replacement_service.take_over(command, context=scenario.replacement_context)

    after = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )
    assert after == before


def _changed_version_or_card_command(command: Any, mutation: str) -> Any:
    """只改变一个服务端确认事实，保持命令本身仍满足 DTO 结构。"""

    if mutation == "batch_version":
        return command.model_copy(
            update={"expected_batch_version": command.expected_batch_version + 1}
        )
    if mutation == "confirmation_card":
        return command.model_copy(update={"confirmation_card_sha256": "0" * 64})
    if mutation == "unit_version":
        decision = command.decisions[0]
        changed_decision = decision.model_copy(
            update={"expected_unit_version": decision.expected_unit_version + 1}
        )
        return command.model_copy(update={"decisions": (changed_decision,)})
    raise AssertionError(f"未知版本或卡片变化：{mutation}")


def _prepare_version_card_operation(
    entry: str,
    *,
    service: Any,
    actor: ImportActorFacts,
    roles: PostgresRoles,
) -> tuple[Any, Any]:
    """返回目标入口和一份由真实合法流程生成的命令。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    marker = uuid4().hex
    if entry == "preflight":
        staged = service.stage_ordinary(
            _zero_write_ordinary_stage_command(marker),
            context=_command_context(actor, idempotency_key=f"zero-write-version-stage-{marker}"),
        )
        return service.preflight, contracts.PreflightImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            expected_mapping_version=1,
        )
    if entry == "confirm":
        command, _ = _ready_single_ordinary_confirmation(
            service,
            actor,
            marker=f"zero-write-version-confirm-{marker}",
        )
        return service.confirm, command
    if entry == "publish_calendar":
        staged = service.stage_authoritative_calendar(
            _zero_write_calendar_stage_command(marker),
            context=_command_context(
                actor,
                idempotency_key=f"zero-write-version-calendar-stage-{marker}",
            ),
        )
        return service.publish_calendar, contracts.ConfirmImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            confirmation_card_sha256=staged.confirmation_card_sha256,
            decisions=(),
        )
    if entry == "resume":
        command, first_context = _ready_single_ordinary_confirmation(
            service,
            actor,
            marker=f"zero-write-version-resume-{marker}",
        )
        _seed_confirmed_person_name(
            roles,
            school_id=actor.school_id,
            display_name="续跑冲突占位教师",
            external_reference=f"zero-write-version-resume-{marker}",
        )
        failed = service.confirm(command, context=first_context)
        assert failed.status == "partially_failed"
        preview = service.preview(
            command.batch_id,
            context=_command_context(
                actor,
                idempotency_key=f"zero-write-version-resume-preview-{marker}",
            ),
        )
        resume_command = contracts.ConfirmImportBatchCommand(
            batch_id=preview.batch_id,
            expected_batch_version=preview.batch_version,
            confirmation_card_sha256=preview.confirmation_card_sha256,
            decisions=tuple(
                contracts.ImportUnitDecision(
                    sequence=unit.sequence,
                    expected_unit_version=unit.unit_version,
                )
                for unit in preview.units
            ),
        )
        return service.resume, resume_command
    raise AssertionError(f"未知版本或卡片入口：{entry}")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("entry", "mutation"),
    [
        ("preflight", "batch_version"),
        ("confirm", "batch_version"),
        ("confirm", "confirmation_card"),
        ("publish_calendar", "batch_version"),
        ("publish_calendar", "confirmation_card"),
        ("resume", "batch_version"),
        ("resume", "confirmation_card"),
    ],
)
def _retired_legacy_test_changed_batch_unit_version_or_card_is_zero_write(
    entry: str,
    mutation: str,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    other_import_actor_facts: ImportActorFacts,
) -> None:
    """批次或确认卡变化时，四类后续写入口均保持双校零写。

    普通 confirm/resume 的单元版本冲突属于可审计的单元执行失败，不是入口拒绝；
    它们不纳入零写矩阵，命令级前置验证的 take_over 在下方单独覆盖该事实。
    """

    service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    operation, original_command = _prepare_version_card_operation(
        entry,
        service=service,
        actor=import_actor_facts,
        roles=current_master_data_schema,
    )
    command = _changed_version_or_card_command(original_command, mutation)
    context = _command_context(
        import_actor_facts,
        idempotency_key=f"task8-zero-write-{entry}-{mutation}-{uuid4().hex}",
    )
    before = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )

    with pytest.raises(application.MasterDataImportVersionConflict):
        operation(command, context=context)

    after = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )
    assert after == before


@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["batch_version", "unit_version", "confirmation_card"])
def _retired_legacy_test_take_over_changed_batch_unit_version_or_card_is_zero_write(
    mutation: str,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """接管命令绑定的批次、单元与卡片任一变化时不写入。"""

    scenario = applying_import_scenario
    scenario.revoke_original_assignment()
    command = scenario.command_with_mutation(mutation)
    before = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )

    with pytest.raises(application.MasterDataImportVersionConflict):
        scenario.replacement_service.take_over(command, context=scenario.replacement_context)

    after = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )
    assert after == before


@pytest.mark.integration
def _retired_legacy_test_preflight_same_key_changed_content_is_zero_write(
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    other_import_actor_facts: ImportActorFacts,
) -> None:
    """预检首请求改变批次状态后，同键异内容只能稳定拒绝且不得再写。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    staged = service.stage_ordinary(
        _zero_write_ordinary_stage_command(marker),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"zero-write-preflight-same-key-stage-{marker}",
        ),
    )
    command = contracts.PreflightImportBatchCommand(
        batch_id=staged.batch_id,
        expected_batch_version=staged.batch_version,
        expected_mapping_version=1,
    )
    idempotency_key = f"task8-zero-write-preflight-same-key-{marker}"
    service.preflight(
        command,
        context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
    )
    changed = command.model_copy(update={"expected_mapping_version": 2})
    before = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )

    with pytest.raises(application.MasterDataImportStateConflict):
        service.preflight(
            changed,
            context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
        )

    after = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )
    assert after == before


def _prepare_completed_idempotent_operation(
    entry: str,
    *,
    service: Any,
    actor: ImportActorFacts,
    roles: PostgresRoles,
) -> tuple[Any, Any]:
    """准备可成功完成且支持可靠同键重放的后续写命令。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    marker = uuid4().hex
    if entry == "confirm":
        command, _ = _ready_single_ordinary_confirmation(
            service,
            actor,
            marker=f"zero-write-same-key-confirm-{marker}",
        )
        return service.confirm, command
    if entry == "publish_calendar":
        staged = service.stage_authoritative_calendar(
            _zero_write_calendar_stage_command(marker),
            context=_command_context(
                actor,
                idempotency_key=f"zero-write-same-key-calendar-stage-{marker}",
            ),
        )
        return service.publish_calendar, contracts.ConfirmImportBatchCommand(
            batch_id=staged.batch_id,
            expected_batch_version=staged.batch_version,
            confirmation_card_sha256=staged.confirmation_card_sha256,
            decisions=(),
        )
    if entry == "resume":
        operation, command = _prepare_version_card_operation(
            "resume",
            service=service,
            actor=actor,
            roles=roles,
        )
        return operation, command
    raise AssertionError(f"未知幂等完成入口：{entry}")


@pytest.mark.integration
@pytest.mark.parametrize("entry", ["confirm", "publish_calendar", "resume"])
def _retired_legacy_test_completed_write_same_key_changed_content_is_zero_write(
    entry: str,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    other_import_actor_facts: ImportActorFacts,
) -> None:
    """确认、发布与续跑完成后，同一幂等键不能绑定另一张确认卡。"""

    service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    operation, command = _prepare_completed_idempotent_operation(
        entry,
        service=service,
        actor=import_actor_facts,
        roles=current_master_data_schema,
    )
    idempotency_key = f"task8-zero-write-{entry}-same-key-{uuid4().hex}"
    operation(
        command,
        context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
    )
    changed = command.model_copy(update={"confirmation_card_sha256": "0" * 64})
    before = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )

    with pytest.raises(application.MasterDataImportIdempotencyConflict):
        operation(
            changed,
            context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
        )

    after = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )
    assert after == before


@pytest.mark.integration
def _retired_legacy_test_take_over_same_key_changed_content_is_zero_write(
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """接管完成后，同键不能替换接管理由或形成第二条接管事实。"""

    scenario = applying_import_scenario
    scenario.revoke_original_assignment()
    scenario.replacement_service.take_over(
        scenario.takeover_command,
        context=scenario.replacement_context,
    )
    changed = scenario.command_with_takeover_reason("同一幂等键不得替换为另一接管理由")
    before = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )

    with pytest.raises(application.MasterDataImportIdempotencyConflict):
        scenario.replacement_service.take_over(changed, context=scenario.replacement_context)

    after = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )
    assert after == before


@pytest.mark.integration
@pytest.mark.parametrize("invalid_state", ["manual", "ordinary_not_applying"])
def _retired_legacy_test_take_over_non_ordinary_or_non_applying_batch_is_zero_write(
    invalid_state: str,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """接管只接受 applying 普通批次，其他同校批次不得形成任何接管副作用。"""

    scenario = applying_import_scenario
    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    actor = scenario.replacement_context.actor
    assert type(actor) is StaffActorContext
    actor_facts = ImportActorFacts(
        school_id=scenario.school_id,
        actor_user_id=actor.account_user_id,
        actor_person_id=actor.person_id,
        actor_assignment_id=actor.assignment_id,
    )
    marker = uuid4().hex
    if invalid_state == "manual":
        preview = scenario.replacement_service.stage_manual(
            contracts.StageManualPersonUnitCommand(
                source_reference=f"zero-write-takeover-manual-{marker}",
                unit=contracts.PersonImportUnitDraft(
                    row_number=1,
                    display_name="不可接管手工教师",
                    external_reference=f"zero-write-takeover-manual-{marker}",
                    valid_from=datetime.now(UTC).replace(microsecond=0),
                ),
            ),
            context=_command_context(
                actor_facts,
                idempotency_key=f"zero-write-takeover-manual-stage-{marker}",
            ),
        )
    else:
        staged = scenario.replacement_service.stage_ordinary(
            _zero_write_ordinary_stage_command(marker),
            context=_command_context(
                actor_facts,
                idempotency_key=f"zero-write-takeover-ready-stage-{marker}",
            ),
        )
        preview = scenario.replacement_service.preflight(
            contracts.PreflightImportBatchCommand(
                batch_id=staged.batch_id,
                expected_batch_version=staged.batch_version,
                expected_mapping_version=1,
            ),
            context=_command_context(
                actor_facts,
                idempotency_key=f"zero-write-takeover-ready-preflight-{marker}",
            ),
        )
    command = scenario.takeover_command.model_copy(
        update={
            "batch_id": preview.batch_id,
            "expected_batch_version": preview.batch_version,
            "confirmation_card_sha256": preview.confirmation_card_sha256,
            "units": (
                scenario.takeover_command.units[0].model_copy(
                    update={"expected_unit_version": preview.units[0].unit_version}
                ),
            ),
        }
    )
    context = _command_context(
        actor_facts,
        idempotency_key=f"task8-zero-write-takeover-{invalid_state}-{marker}",
    )
    before = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )

    with pytest.raises(application.MasterDataImportStateConflict):
        scenario.replacement_service.take_over(command, context=context)

    after = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )
    assert after == before


@pytest.mark.integration
def _retired_legacy_test_take_over_terminal_batch_is_zero_write(
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """一次接管已完成后，另一命令不能再次接管该终态批次。"""

    scenario = applying_import_scenario
    scenario.revoke_original_assignment()
    scenario.replacement_service.take_over(
        scenario.takeover_command,
        context=scenario.replacement_context,
    )
    actor = scenario.replacement_context.actor
    assert type(actor) is StaffActorContext
    facts = ImportActorFacts(
        school_id=scenario.school_id,
        actor_user_id=actor.account_user_id,
        actor_person_id=actor.person_id,
        actor_assignment_id=actor.assignment_id,
    )
    context = _command_context(
        facts,
        idempotency_key=f"task8-zero-write-second-terminal-takeover-{uuid4().hex}",
    )
    before = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )

    with pytest.raises(application.MasterDataImportStateConflict):
        scenario.replacement_service.take_over(scenario.takeover_command, context=context)

    after = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )
    assert after == before


@pytest.mark.integration
@pytest.mark.parametrize("rejection", ["changed_card", "same_key_changed_content"])
def _retired_legacy_test_manual_confirm_card_or_same_key_conflict_is_zero_write(
    rejection: str,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    other_import_actor_facts: ImportActorFacts,
) -> None:
    """手工确认分支的卡片和幂等拒绝同样保持两个学校零写入。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    marker = uuid4().hex
    staged = service.stage_manual(
        contracts.StageManualPersonUnitCommand(
            source_reference=f"zero-write-manual-confirm-{marker}",
            unit=contracts.PersonImportUnitDraft(
                row_number=1,
                display_name="手工确认零写教师",
                external_reference=f"zero-write-manual-confirm-{marker}",
                valid_from=datetime.now(UTC).replace(microsecond=0),
            ),
        ),
        context=_command_context(
            import_actor_facts,
            idempotency_key=f"zero-write-manual-confirm-stage-{marker}",
        ),
    )
    command = contracts.ConfirmImportBatchCommand(
        batch_id=staged.batch_id,
        expected_batch_version=staged.batch_version,
        confirmation_card_sha256=staged.confirmation_card_sha256,
        decisions=(),
    )
    idempotency_key = f"task8-zero-write-manual-confirm-{rejection}-{marker}"
    if rejection == "same_key_changed_content":
        service.confirm(
            command,
            context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
        )
    changed = command.model_copy(update={"confirmation_card_sha256": "0" * 64})
    before = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )

    expected = (
        application.MasterDataImportIdempotencyConflict
        if rejection == "same_key_changed_content"
        else application.MasterDataImportVersionConflict
    )
    with pytest.raises(expected):
        service.confirm(
            changed,
            context=_command_context(import_actor_facts, idempotency_key=idempotency_key),
        )

    after = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )
    assert after == before


@pytest.mark.integration
@pytest.mark.parametrize("entry", ["preflight", "confirm", "publish_calendar", "resume"])
def _retired_legacy_test_terminal_batch_rejected_by_followup_entry_is_zero_write(
    entry: str,
    current_master_data_schema: PostgresRoles,
    import_runtime_session_factory: sessionmaker[Session],
    import_actor_facts: ImportActorFacts,
    other_import_actor_facts: ImportActorFacts,
) -> None:
    """已经完成的批次不能被预检、确认、发布或续跑再次改变。"""

    contracts = importlib.import_module("app.platform.master_data_imports.contracts")
    service, _ = _import_service_and_uow_factory(import_runtime_session_factory)
    if entry == "preflight":
        terminal_command, terminal_context = _ready_single_ordinary_confirmation(
            service,
            import_actor_facts,
            marker=f"zero-write-terminal-preflight-{uuid4().hex}",
        )
        service.confirm(terminal_command, context=terminal_context)
        operation = service.preflight
        command = contracts.PreflightImportBatchCommand(
            batch_id=terminal_command.batch_id,
            expected_batch_version=terminal_command.expected_batch_version,
            expected_mapping_version=1,
        )
    else:
        operation, command = _prepare_completed_idempotent_operation(
            entry,
            service=service,
            actor=import_actor_facts,
            roles=current_master_data_schema,
        )
        if entry == "resume":
            with current_master_data_schema.migration_connection() as connection:
                connection.execute("SET ROLE campus_ai_db_owner")
                released = connection.execute(
                    """
                    UPDATE school_person_names AS person_name
                    SET source_ref = person_name.source_ref || '-released',
                        version = person_name.version + 1
                    FROM master_data_import_units AS import_unit
                    WHERE import_unit.school_id = %s
                      AND import_unit.batch_id = %s
                      AND person_name.school_id = import_unit.school_id
                      AND person_name.source_ref = import_unit.payload->>'external_reference'
                    """,
                    (import_actor_facts.school_id, command.batch_id),
                ).rowcount
            assert released == 1
        operation(
            command,
            context=_command_context(
                import_actor_facts,
                idempotency_key=f"zero-write-terminal-complete-{entry}-{uuid4().hex}",
            ),
        )
    before = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )

    with pytest.raises(application.MasterDataImportStateConflict):
        operation(
            command,
            context=_command_context(
                import_actor_facts,
                idempotency_key=f"task8-zero-write-terminal-reject-{entry}-{uuid4().hex}",
            ),
        )

    after = _dual_import_write_snapshot(
        current_master_data_schema,
        import_actor_facts.school_id,
        other_import_actor_facts.school_id,
    )
    assert after == before


@pytest.mark.integration
@pytest.mark.parametrize("target_facts", ["old_active", "existing_takeover"])
def _retired_legacy_test_cross_school_old_active_or_existing_takeover_is_zero_write(
    target_facts: str,
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """他校管理员不能终止旧活动命令，也不能触碰已有接管链。"""

    scenario = applying_import_scenario
    if target_facts == "existing_takeover":
        scenario.revoke_original_assignment()
        scenario.replacement_service.take_over(
            scenario.takeover_command,
            context=scenario.replacement_context,
        )
    cross_actor = _seed_same_school_import_actor(
        scenario.roles,
        school_id=scenario.cross_school_id,
    )
    cross_service, _ = _import_service_and_uow_factory(scenario.session_factory)
    context = _command_context(
        cross_actor,
        idempotency_key=f"task8-zero-write-cross-{target_facts}-{uuid4().hex}",
    )
    before = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )

    with pytest.raises(application.MasterDataImportStateConflict):
        cross_service.take_over(scenario.takeover_command, context=context)

    after = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )
    assert after == before


@pytest.mark.integration
def _retired_legacy_test_successful_takeover_replay_reauthorizes_current_semantic_role_with_zero_write(  # noqa: E501
    applying_import_scenario: ApplyingImportScenario,
) -> None:
    """接管重放不能用历史成功结果绕过当前任职语义角色重验。"""

    scenario = applying_import_scenario
    scenario.revoke_original_assignment()
    scenario.replacement_service.take_over(
        scenario.takeover_command,
        context=scenario.replacement_context,
    )
    replacement_actor = scenario.replacement_context.actor
    assert type(replacement_actor) is StaffActorContext
    with scenario.roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        changed = connection.execute(
            """
            UPDATE staff_assignments
            SET semantic_role_code = 'teacher'
            WHERE school_id = %s AND id = %s
            """,
            (scenario.school_id, replacement_actor.assignment_id),
        ).rowcount
        content_changed = connection.execute(
            "UPDATE staff_assignment_versions SET semantic_role_code = 'teacher' "
            "WHERE school_id = %s AND assignment_id = %s",
            (scenario.school_id, replacement_actor.assignment_id),
        ).rowcount
    assert changed == 1
    assert content_changed >= 1
    before = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )

    with pytest.raises(application.MasterDataImportPermissionDenied):
        scenario.replacement_service.take_over(
            scenario.takeover_command,
            context=scenario.replacement_context,
        )

    after = _dual_import_write_snapshot(
        scenario.roles,
        scenario.school_id,
        scenario.cross_school_id,
    )
    assert after == before
