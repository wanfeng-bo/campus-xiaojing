from __future__ import annotations

import threading
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.domains.campus import models as campus_models  # noqa: F401
from app.domains.students import models as student_models  # noqa: F401
from app.platform import contracts as platform_contracts
from app.platform.audit.contracts import BusinessAuditRecord, BusinessAuditSummary
from app.platform.audit.models import AuditLog
from app.platform.audit.service import append_business_audit_log
from app.platform.contracts import (
    AssignmentId,
    PersonId,
    SchoolId,
)
from app.platform.directory import models as directory_models  # noqa: F401
from app.platform.idempotency.contracts import (
    CommandIdempotencyIdentity,
    IdempotencyPayloadConflict,
    SafeCommandResult,
)
from app.platform.idempotency.models import CommandIdempotencyRecord
from app.platform.idempotency.repository import SqlAlchemyCommandIdempotencyRepository
from app.platform.identity import models as identity_models  # noqa: F401
from app.platform.messaging.contracts import OutboxEvent
from app.platform.messaging.models import OutboxMessage
from app.platform.messaging.repository import SqlAlchemyOutboxWriter
from app.platform.organization import models as organization_models  # noqa: F401
from app.platform.tenancy import models as tenancy_models  # noqa: F401
from tests.master_data.conftest import PostgresRoles
from tests.master_data.test_core_schema import (
    _foreign_key_contract,  # pyright: ignore[reportPrivateUsage]
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_business_audit_summary_rejects_sensitive_or_unstructured_fields() -> None:
    """新业务审计不能把姓名、照片或任意映射复制进长期审计。"""

    with pytest.raises(ValueError):
        BusinessAuditSummary.model_validate(
            {
                "state_code": "assignment.confirmed",
                "student_name": "李明",
            }
        )
    with pytest.raises(ValueError):
        BusinessAuditSummary.model_validate(
            {
                "state_code": "assignment.confirmed",
                "photo_url": "https://example.invalid/private.jpg",
            }
        )


def test_business_audit_record_only_accepts_stable_codes_and_uuid_reference() -> None:
    """事件、对象和原因字段也不能成为姓名、电话或 URL 的旁路。"""

    summary = BusinessAuditSummary(state_code="assignment.confirmed")
    with pytest.raises(ValueError):
        BusinessAuditRecord.model_validate(
            {
                "event_type": "学生李明已调班",
                "target_type": "staff_assignment",
                "target_id": str(uuid4()),
                "before_summary": summary.model_dump(mode="json"),
                "after_summary": summary.model_dump(mode="json"),
                "reason_code": "manual.confirmed",
            }
        )
    with pytest.raises(ValueError):
        BusinessAuditRecord.model_validate(
            {
                "event_type": "master_data.assignment.changed",
                "target_type": "staff_assignment",
                "target_id": "https://example.invalid/private.jpg",
                "before_summary": summary.model_dump(mode="json"),
                "after_summary": summary.model_dump(mode="json"),
                "reason_code": "manual.confirmed",
            }
        )
    with pytest.raises(ValueError):
        BusinessAuditRecord.model_validate(
            {
                "event_type": "master_data.assignment.changed",
                "target_type": "staff_assignment",
                "target_id": uuid4(),
                "before_summary": summary.model_dump(mode="json"),
                "after_summary": summary.model_dump(mode="json"),
                "reason_code": "电话13800000000",
            }
        )
    with pytest.raises(ValueError):
        BusinessAuditRecord.model_validate(
            {
                "event_type": "master_data.assignment.changed",
                "target_type": "staff_assignment",
                "target_id": uuid4(),
                "before_summary": summary,
                "after_summary": summary,
                "reason_code": "phone.13800000000",
            }
        )
    with pytest.raises(ValueError):
        BusinessAuditSummary.model_validate(
            {
                "state_code": "assignment.confirmed",
                "fact_codes": ("phone.13800000000",),
            }
        )


def _seed_staff_facts(
    session: Session,
    *,
    school_id: UUID | None = None,
) -> tuple[UUID, int, UUID, UUID, UUID]:
    selected_school_id = school_id or uuid4()
    person_id = uuid4()
    unit_id = uuid4()
    assignment_id = uuid4()
    marker = uuid4().hex
    binding_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO schools (
                id, school_code, display_name, timezone_name, status, version
            ) VALUES (:school_id, :code, 'Task 5 事务学校', 'Asia/Shanghai', 'active', 1)
            """
        ),
        {"school_id": selected_school_id, "code": f"task5-transaction-{marker}"},
    )
    user_id = session.execute(
        text(
            """
            INSERT INTO users (external_subject, display_name, is_active, security_version)
            VALUES (:subject, 'Task 5 操作者', true, 1)
            RETURNING id
            """
        ),
        {"subject": f"task5:transaction:{marker}"},
    ).scalar_one()
    session.execute(
        text(
            """
            INSERT INTO school_people (id, school_id, status, version)
            VALUES (:person_id, :school_id, 'active', 1)
            """
        ),
        {"person_id": person_id, "school_id": selected_school_id},
    )
    session.execute(
        text(
            """
            INSERT INTO account_person_bindings (
                id, school_id, source_key, account_user_id, person_id,
                valid_from, status, version
            ) VALUES (
                :id, :school_id, :source_key, :user_id, :person_id,
                :valid_from, 'confirmed', 1
            )
            """
        ),
        {
            "id": binding_id,
            "school_id": selected_school_id,
            "source_key": f"login:{marker}",
            "user_id": int(user_id),
            "person_id": person_id,
            "valid_from": T0,
        },
    )
    session.execute(
        text(
            """
            INSERT INTO organization_units (id, school_id, kind, status, version)
            VALUES (:unit_id, :school_id, 'department', 'active', 1)
            """
        ),
        {"unit_id": unit_id, "school_id": selected_school_id},
    )
    session.execute(
        text(
            """
            INSERT INTO staff_assignments (
                id, school_id, person_id, organization_unit_id, position_title,
                semantic_role_code, appointment_source, approval_reference,
                valid_from, status, version
            ) VALUES (
                :assignment_id, :school_id, :person_id, :unit_id, '维修人员',
                'repair_worker', 'manual', :approval_reference,
                :valid_from, 'confirmed', 1
            )
            """
        ),
        {
            "assignment_id": assignment_id,
            "school_id": selected_school_id,
            "person_id": person_id,
            "unit_id": unit_id,
            "approval_reference": f"task5-{marker}",
            "valid_from": T0,
        },
    )
    if session.execute(
        text("SELECT to_regclass('public.staff_assignment_versions') IS NOT NULL")
    ).scalar_one():
        source_id = uuid4()
        source_ref = f"test:task5-assignment-content:{marker}"
        session.execute(
            text(
                "INSERT INTO school_fact_sources "
                "(id, school_id, source_key, source_kind, source_ref, schema_version) "
                "VALUES (:id, :school_id, :source_key, 'legacy', :source_ref, 1)"
            ),
            {
                "id": source_id,
                "school_id": selected_school_id,
                "source_key": source_ref,
                "source_ref": source_ref,
            },
        )
        session.execute(
            text(
                "INSERT INTO staff_assignment_versions "
                "(id, school_id, assignment_id, person_id, organization_unit_id, "
                "position_title, semantic_role_code, assignment_kind, appointment_source, "
                "approval_reference, effective_from, status, target_version, source_ref, "
                "source_id) VALUES "
                "(:id, :school_id, :assignment_id, :person_id, :unit_id, '维修人员', "
                "'repair_worker', 'substantive', 'manual', :approval_reference, "
                ":effective_from, 'confirmed', 1, :source_ref, :source_id)"
            ),
            {
                "id": uuid4(),
                "school_id": selected_school_id,
                "assignment_id": assignment_id,
                "person_id": person_id,
                "unit_id": unit_id,
                "approval_reference": f"task5-{marker}",
                "effective_from": T0,
                "source_ref": source_ref,
                "source_id": source_id,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO account_person_binding_versions (
                    id, school_id, binding_id, account_user_id, person_id,
                    provider_code, provider_tenant_key, external_subject,
                    effective_from, status, target_version, source_ref, source_id
                ) VALUES (
                    :id, :school_id, :binding_id, :user_id, :person_id,
                    'legacy', 'legacy', :external_subject,
                    :effective_from, 'confirmed', 1, :source_ref, :source_id
                )
                """
            ),
            {
                "id": uuid4(),
                "school_id": selected_school_id,
                "binding_id": binding_id,
                "user_id": int(user_id),
                "person_id": person_id,
                "external_subject": f"task5:transaction:{marker}",
                "effective_from": T0,
                "source_ref": source_ref,
                "source_id": source_id,
            },
        )
    return selected_school_id, int(user_id), person_id, unit_id, assignment_id


def _identity(
    *,
    school_id: UUID,
    user_id: int,
    person_id: UUID,
    assignment_id: UUID,
    request_sha256: str = "b" * 64,
) -> CommandIdempotencyIdentity:
    return CommandIdempotencyIdentity(
        school_id=SchoolId(school_id),
        action_code="master_data.assignment.change",
        actor_user_id=user_id,
        actor_person_id=PersonId(person_id),
        actor_context_type="assignment",
        actor_assignment_id=AssignmentId(assignment_id),
        actor_assignment_version=1,
        resource_type="staff_assignment",
        resource_id=str(assignment_id),
        idempotency_key_hash="a" * 64,
        request_sha256=request_sha256,
    )


@pytest.mark.integration
def test_idempotency_outbox_and_typed_audit_share_caller_transaction_without_commit(
    current_owner_session: Session,
    current_master_data_schema: PostgresRoles,
) -> None:
    """幂等、业务审计和 Outbox 只 flush，同一外层事务失败时可整体回滚。"""

    school_id, user_id, person_id, _, assignment_id = _seed_staff_facts(current_owner_session)
    identity = _identity(
        school_id=school_id,
        user_id=user_id,
        person_id=person_id,
        assignment_id=assignment_id,
    )
    idempotency = SqlAlchemyCommandIdempotencyRepository(current_owner_session)
    first = idempotency.acquire(identity)
    replay = idempotency.acquire(identity)
    assert replay.record_id == first.record_id

    with pytest.raises(IdempotencyPayloadConflict):
        idempotency.acquire(
            _identity(
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                request_sha256="c" * 64,
            )
        )

    result = SafeCommandResult(
        resource_type="staff_assignment",
        resource_id=assignment_id,
        resource_version=2,
        outcome_code="updated",
    )
    completed = idempotency.mark_succeeded(
        identity,
        result_type="master_data.assignment.result",
        result=result,
    )
    assert completed.status == "succeeded"

    event = OutboxEvent(
        event_id=uuid4(),
        school_id=SchoolId(school_id),
        event_key=f"assignment.changed:{assignment_id}",
        event_type="master_data.assignment.changed",
        aggregate_type="staff_assignment",
        aggregate_id=assignment_id,
        aggregate_version=2,
        occurred_at=T0,
    )
    SqlAlchemyOutboxWriter(current_owner_session).append(event)

    actor = platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=user_id,
        school_id=SchoolId(school_id),
        person_id=PersonId(person_id),
        assignment_id=AssignmentId(assignment_id),
        assignment_version=1,
        authorization_version=1,
    )
    command = platform_contracts._issue_command_execution_context(  # pyright: ignore[reportPrivateUsage]
        actor=actor,
        request_id=uuid4(),
        idempotency_key="client-secret-idempotency-key",
    )
    audit = append_business_audit_log(
        session=current_owner_session,
        context=command,
        record=BusinessAuditRecord(
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
        ),
    )
    assert audit.idempotency_key_hash == sha256(b"client-secret-idempotency-key").hexdigest()
    audit_id = audit.audit_id
    assert (
        current_owner_session.scalar(
            select(func.count())
            .select_from(CommandIdempotencyRecord)
            .where(CommandIdempotencyRecord.id == first.record_id)
        )
        == 1
    )
    assert (
        current_owner_session.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.id == event.event_id)
        )
        == 1
    )
    assert (
        current_owner_session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.id == audit_id)
        )
        == 1
    )
    assert current_owner_session.in_transaction()

    with current_master_data_schema.migration_connection() as observer:
        observer.execute("SET ROLE campus_ai_db_owner")
        assert observer.execute(
            "SELECT count(*) FROM command_idempotency_records WHERE id = %s",
            (first.record_id,),
        ).fetchone() == (0,)
        assert observer.execute(
            "SELECT count(*) FROM outbox_messages WHERE id = %s",
            (event.event_id,),
        ).fetchone() == (0,)

    with pytest.raises(IntegrityError, match="ck_outbox_messages_safe_payload"):
        current_owner_session.execute(
            text(
                """
                INSERT INTO outbox_messages (
                    id, school_id, event_key, event_type, aggregate_type,
                    aggregate_id, aggregate_version, occurred_at, payload
                ) VALUES (
                    :id, :school_id, :event_key, 'master_data.assignment.changed',
                    'staff_assignment', :aggregate_id, 2, :occurred_at,
                    jsonb_build_object(
                        'event_type', 'master_data.assignment.changed',
                        'aggregate_type', 'staff_assignment',
                        'aggregate_id', CAST(:aggregate_id AS text),
                        'aggregate_version', 2,
                        'occurred_at', CAST(:occurred_at AS text),
                        'student_name', '不应进入事务事件'
                    )
                )
                """
            ),
            {
                "id": uuid4(),
                "school_id": school_id,
                "event_key": f"assignment.changed:invalid:{uuid4()}",
                "aggregate_id": assignment_id,
                "occurred_at": T0,
            },
        )
    current_owner_session.rollback()
    with current_master_data_schema.migration_connection() as observer:
        observer.execute("SET ROLE campus_ai_db_owner")
        assert observer.execute(
            "SELECT count(*) FROM command_idempotency_records WHERE id = %s",
            (first.record_id,),
        ).fetchone() == (0,)
        assert observer.execute(
            "SELECT count(*) FROM outbox_messages WHERE id = %s",
            (event.event_id,),
        ).fetchone() == (0,)
        assert observer.execute(
            "SELECT count(*) FROM audit_logs WHERE id = %s",
            (audit_id,),
        ).fetchone() == (0,)


@pytest.mark.integration
def test_concurrent_idempotency_acquire_and_response_loss_replay_converge(
    current_master_data_schema: PostgresRoles,
) -> None:
    """两个真实事务只创建一行；提交后丢失响应可从新事务取得同一安全结果。"""

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
    with Session(engine) as setup:
        setup.execute(text("SET ROLE campus_ai_db_owner"))
        school_id, user_id, person_id, _, assignment_id = _seed_staff_facts(setup)
        setup.commit()
    identity = _identity(
        school_id=school_id,
        user_id=user_id,
        person_id=person_id,
        assignment_id=assignment_id,
    )
    start = threading.Barrier(2)
    lock = threading.Lock()
    record_ids: list[UUID] = []
    errors: list[BaseException] = []

    def acquire_once() -> None:
        try:
            with Session(engine) as session:
                session.execute(text("SET ROLE campus_ai_db_owner"))
                session.execute(text("SET LOCAL lock_timeout = '5s'"))
                start.wait(timeout=5)
                projection = SqlAlchemyCommandIdempotencyRepository(session).acquire(identity)
                session.commit()
            with lock:
                record_ids.append(projection.record_id)
        except BaseException as error:  # noqa: BLE001 - 测试线程必须把全部异常带回主线程
            with lock:
                errors.append(error)

    first_thread = threading.Thread(target=acquire_once)
    second_thread = threading.Thread(target=acquire_once)
    first_thread.start()
    second_thread.start()
    first_thread.join(timeout=10)
    second_thread.join(timeout=10)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert len(record_ids) == 2
    assert len(set(record_ids)) == 1

    result = SafeCommandResult(
        resource_type="staff_assignment",
        resource_id=assignment_id,
        resource_version=2,
        outcome_code="updated",
    )
    with Session(engine) as finisher:
        finisher.execute(text("SET ROLE campus_ai_db_owner"))
        completed = SqlAlchemyCommandIdempotencyRepository(finisher).mark_succeeded(
            identity,
            result_type="master_data.assignment.result",
            result=result,
        )
        assert completed.status == "succeeded"
        finisher.commit()
    with Session(engine) as replay_session:
        replay_session.execute(text("SET ROLE campus_ai_db_owner"))
        replay = SqlAlchemyCommandIdempotencyRepository(replay_session).acquire(identity)
        assert replay.record_id == record_ids[0]
        assert replay.status == "succeeded"
        assert replay.result == result
        assert (
            replay_session.scalar(
                select(func.count())
                .select_from(CommandIdempotencyRecord)
                .where(CommandIdempotencyRecord.id == record_ids[0])
            )
            == 1
        )
        replay_session.rollback()
    engine.dispose()


@pytest.mark.integration
def test_database_rejects_unsafe_outbox_and_mixed_idempotency_payloads(
    current_owner_session: Session,
) -> None:
    """直接 SQL 也不能把额外敏感字段塞进 Outbox 或混合两类 actor。"""

    school_id, user_id, person_id, _, assignment_id = _seed_staff_facts(current_owner_session)
    with (
        pytest.raises(IntegrityError, match="ck_outbox_messages_safe_payload"),
        current_owner_session.begin_nested(),
    ):
        current_owner_session.execute(
            text(
                """
                INSERT INTO outbox_messages (
                    id, school_id, event_key, event_type, aggregate_type,
                    aggregate_id, aggregate_version, occurred_at, payload, status
                ) VALUES (
                    :id, :school_id, :event_key, 'master_data.assignment.changed',
                    'staff_assignment', :aggregate_id, 2, :occurred_at,
                    CAST(:payload AS jsonb), 'pending'
                )
                """
            ),
            {
                "id": uuid4(),
                "school_id": school_id,
                "event_key": f"unsafe:{uuid4()}",
                "aggregate_id": assignment_id,
                "occurred_at": T0,
                "payload": (
                    '{"event_type":"master_data.assignment.changed",'
                    '"aggregate_type":"staff_assignment",'
                    f'"aggregate_id":"{assignment_id}",'
                    '"aggregate_version":2,"occurred_at":"2026-01-01T00:00:00+00:00",'
                    '"student_name":"不应进入Outbox"}'
                ),
            },
        )

    with (
        pytest.raises(IntegrityError, match="ck_outbox_messages_safe_payload"),
        current_owner_session.begin_nested(),
    ):
        current_owner_session.execute(
            text(
                """
                INSERT INTO outbox_messages (
                    id, school_id, event_key, event_type, aggregate_type,
                    aggregate_id, aggregate_version, occurred_at, payload, status
                ) VALUES (
                    :id, :school_id, :event_key, 'master_data.assignment.changed',
                    'staff_assignment', :aggregate_id, 2, :occurred_at,
                    CAST(:payload AS jsonb), 'pending'
                )
                """
            ),
            {
                "id": uuid4(),
                "school_id": school_id,
                "event_key": f"mismatch:{uuid4()}",
                "aggregate_id": assignment_id,
                "occurred_at": T0,
                "payload": (
                    '{"event_type":"master_data.assignment.changed",'
                    '"aggregate_type":"staff_assignment",'
                    f'"aggregate_id":"{uuid4()}",'
                    '"aggregate_version":2,'
                    '"occurred_at":"2026-01-01T00:00:00+00:00"}'
                ),
            },
        )

    with (
        pytest.raises(IntegrityError, match="ck_command_idempotency_records_actor_context"),
        current_owner_session.begin_nested(),
    ):
        current_owner_session.execute(
            text(
                """
                INSERT INTO command_idempotency_records (
                    id, school_id, action_code, actor_user_id, actor_person_id,
                    actor_context_type, actor_context_key,
                    actor_assignment_id, actor_assignment_version,
                    actor_guardian_relationship_id, actor_guardian_relationship_version,
                    resource_type, resource_id, idempotency_key_hash,
                    request_sha256, status
                ) VALUES (
                    :id, :school_id, 'master_data.assignment.change', :user_id, :person_id,
                    'assignment', :context_key, :assignment_id, 1, :guardian_id, 1,
                    'staff_assignment', :resource_id, :key_hash, :request_hash, 'in_progress'
                )
                """
            ),
            {
                "id": uuid4(),
                "school_id": school_id,
                "user_id": user_id,
                "person_id": person_id,
                "context_key": f"assignment:{assignment_id}:1",
                "assignment_id": assignment_id,
                "guardian_id": uuid4(),
                "resource_id": str(assignment_id),
                "key_hash": "a" * 64,
                "request_hash": "b" * 64,
            },
        )

    with (
        pytest.raises(IntegrityError, match="ck_command_idempotency_records_actor_context"),
        current_owner_session.begin_nested(),
    ):
        current_owner_session.execute(
            text(
                """
                INSERT INTO command_idempotency_records (
                    id, school_id, action_code, actor_user_id, actor_person_id,
                    actor_context_type, actor_context_key,
                    actor_assignment_id, actor_assignment_version,
                    resource_type, resource_id, idempotency_key_hash,
                    request_sha256, status
                ) VALUES (
                    :id, :school_id, 'master_data.assignment.change', :user_id, :person_id,
                    'assignment', 'assignment:forged:1', :assignment_id, 1,
                    'staff_assignment', :resource_id, :key_hash, :request_hash, 'in_progress'
                )
                """
            ),
            {
                "id": uuid4(),
                "school_id": school_id,
                "user_id": user_id,
                "person_id": person_id,
                "assignment_id": assignment_id,
                "resource_id": str(assignment_id),
                "key_hash": "c" * 64,
                "request_hash": "d" * 64,
            },
        )


@pytest.mark.integration
def test_responsibility_membership_is_bound_to_same_school_formal_assignment(
    current_owner_connection: psycopg.Connection[object],
) -> None:
    """存量责任表仍以复合外键绑定同校正式任职。"""

    assert _foreign_key_contract(
        current_owner_connection,
        table_name="responsibility_groups",
        constraint_name="fk_responsibility_groups_manager_assignment",
    ) == (
        ("school_id", "manager_assignment_id"),
        "staff_assignments",
        ("school_id", "id"),
        "r",
    )
    assert _foreign_key_contract(
        current_owner_connection,
        table_name="responsibility_group_memberships",
        constraint_name="fk_responsibility_group_memberships_assignment",
    ) == (
        ("school_id", "assignment_id"),
        "staff_assignments",
        ("school_id", "id"),
        "r",
    )
