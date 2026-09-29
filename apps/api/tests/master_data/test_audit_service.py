from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, LiteralString

import psycopg
import pytest
from psycopg import sql
from pydantic import ValidationError
from sqlalchemy import URL, Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.platform.audit.contracts import BusinessAuditRecord, BusinessAuditSummary
from app.platform.audit.service import append_audit_log
from app.platform.identity.models import User
from tests.master_data.conftest import PostgresRoles


def test_master_data_assignment_typed_audit_requires_the_complete_transition_tuple() -> None:
    """主数据任职确认只能使用当前领域实际产生的完整事件组合。"""

    assignment_id = uuid.uuid4()
    record = BusinessAuditRecord(
        event_type="master_data.assignment.changed",
        target_type="staff_assignment",
        target_id=assignment_id,
        before_summary=BusinessAuditSummary(
            state_code="assignment.absent",
            stable_reference_ids=(assignment_id,),
        ),
        after_summary=BusinessAuditSummary(
            state_code="assignment.confirmed",
            resource_version=1,
            stable_reference_ids=(assignment_id,),
            fact_codes=("assignment.updated",),
        ),
        reason_code="manual.assignment-confirmed",
    )

    assert record.target_id == assignment_id

    stale_update = record.model_copy(
        update={
            "before_summary": record.before_summary.model_copy(
                update={"state_code": "assignment.confirmed", "resource_version": 1}
            ),
            "after_summary": record.after_summary.model_copy(update={"resource_version": 2}),
        }
    )
    with pytest.raises(ValidationError):
        BusinessAuditRecord.model_validate(stale_update.model_dump())


def test_repair_submission_typed_audit_requires_the_complete_target_tuple() -> None:
    ticket_id = uuid.uuid4()
    record = BusinessAuditRecord(
        event_type="repair.ticket.submitted",
        target_type="repair_ticket",
        target_id=ticket_id,
        before_summary=BusinessAuditSummary(
            state_code="repair.absent",
            stable_reference_ids=(ticket_id,),
        ),
        after_summary=BusinessAuditSummary(
            state_code="repair.submitted",
            resource_version=1,
            stable_reference_ids=(ticket_id,),
            fact_codes=("repair.submitted",),
        ),
        reason_code="manual.repair-submitted",
    )

    assert record.target_id == ticket_id

    for broken in (
        record.model_copy(
            update={
                "before_summary": record.before_summary.model_copy(
                    update={"stable_reference_ids": ()}
                )
            }
        ),
        record.model_copy(
            update={
                "after_summary": record.after_summary.model_copy(update={"resource_version": 2})
            }
        ),
        record.model_copy(
            update={
                "after_summary": record.after_summary.model_copy(
                    update={"stable_reference_ids": (uuid.uuid4(),)}
                )
            }
        ),
    ):
        with pytest.raises(ValidationError):
            BusinessAuditRecord.model_validate(broken.model_dump())


def _runtime_engine(postgres_roles: PostgresRoles) -> Engine:
    runtime = postgres_roles.runtime_connection_parameters
    return create_engine(
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


@contextmanager
def _owner_connection(
    postgres_roles: PostgresRoles,
) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(
        **postgres_roles.migration_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        yield connection


def _create_committed_audit_log(postgres_roles: PostgresRoles) -> tuple[int, int]:
    """runtime 只通过正式窄函数创建一条已提交的兼容审计。"""

    marker = uuid.uuid4().hex
    engine = _runtime_engine(postgres_roles)
    try:
        with Session(engine) as session:
            actor = User(
                external_subject=f"audit-mutation:{marker}",
                display_name="变更测试管理员",
            )
            session.add(actor)
            session.flush()
            audit_log = append_audit_log(
                session=session,
                actor_user_id=actor.id,
                event_type="authorization.scope.assigned",
                target_type="responsibility_scope",
                target_id="42",
                before_summary={},
                after_summary={"scope_type": "location", "scope_value": "building-a"},
                request_id=str(uuid.uuid4()),
                reason="验证审计不可变保护",
            )
            actor_id = actor.id
            audit_log_id = audit_log.id
            session.commit()
    finally:
        engine.dispose()
    return audit_log_id, actor_id


@pytest.mark.integration
def test_append_audit_log_only_adds_a_complete_record(
    current_master_data_schema: PostgresRoles,
) -> None:
    audit_log_id, actor_id = _create_committed_audit_log(current_master_data_schema)

    with _owner_connection(current_master_data_schema) as connection:
        saved = connection.execute(
            """
            SELECT actor_user_id, event_type, after_summary, reason
            FROM public.audit_logs
            WHERE id = %s
            """,
            (audit_log_id,),
        ).fetchone()

    assert saved == (
        actor_id,
        "authorization.scope.assigned",
        {"scope_type": "location", "scope_value": "building-a"},
        "验证审计不可变保护",
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE public.audit_logs SET reason = '不应成功' WHERE id = %s",
        "DELETE FROM public.audit_logs WHERE id = %s",
    ],
    ids=["update", "delete"],
)
def test_database_rejects_existing_audit_log_mutation(
    current_master_data_schema: PostgresRoles,
    statement: LiteralString,
) -> None:
    """即使 Owner 直接执行 SQL，也必须由数据库触发器拒绝历史改写。"""

    audit_log_id, _ = _create_committed_audit_log(current_master_data_schema)

    with pytest.raises(psycopg.DatabaseError, match="audit_logs are immutable") as error:
        with _owner_connection(current_master_data_schema) as connection:
            connection.execute(sql.SQL(statement), (audit_log_id,))
    assert error.value.sqlstate == "55000"

    with _owner_connection(current_master_data_schema) as connection:
        saved_reason = connection.execute(
            "SELECT reason FROM public.audit_logs WHERE id = %s",
            (audit_log_id,),
        ).fetchone()
    assert saved_reason == ("验证审计不可变保护",)


@pytest.mark.integration
def test_database_rejects_deleting_audited_actor_at_foreign_key_boundary(
    current_master_data_schema: PostgresRoles,
) -> None:
    """已被审计引用的操作者必须由 RESTRICT 外键拒绝删除。"""

    audit_log_id, actor_user_id = _create_committed_audit_log(current_master_data_schema)

    with pytest.raises(psycopg.DatabaseError) as error:
        with _owner_connection(current_master_data_schema) as connection:
            connection.execute("DELETE FROM public.users WHERE id = %s", (actor_user_id,))
    assert error.value.sqlstate == "23503"

    with _owner_connection(current_master_data_schema) as connection:
        saved_actor = connection.execute(
            "SELECT actor_user_id FROM public.audit_logs WHERE id = %s",
            (audit_log_id,),
        ).fetchone()
    assert saved_actor == (actor_user_id,)
