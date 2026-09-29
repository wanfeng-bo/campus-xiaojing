from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CommandIdempotencyRecord(Base):
    """保存命令首次执行状态和不含敏感快照的稳定结果。"""

    __tablename__ = "command_idempotency_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "actor_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_command_idempotency_records_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "actor_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_command_idempotency_records_assignment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "actor_guardian_relationship_id"],
            ["guardian_relationships.school_id", "guardian_relationships.id"],
            name="fk_command_idempotency_records_guardian",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('in_progress', 'succeeded', 'terminal_failure')",
            name="ck_command_idempotency_records_status",
        ),
        CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$' AND request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_command_idempotency_records_hashes",
        ),
        CheckConstraint(
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
        CheckConstraint(
            "(status = 'in_progress' AND result_type IS NULL AND result_payload IS NULL) OR "
            "(status IN ('succeeded', 'terminal_failure') "
            "AND result_type IS NOT NULL AND result_payload IS NOT NULL)",
            name="ck_command_idempotency_records_result_state",
        ),
        CheckConstraint(
            "result_payload IS NULL OR ("
            "jsonb_typeof(result_payload) = 'object' "
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
        UniqueConstraint(
            "school_id",
            "id",
            name="uq_command_idempotency_records_school_id_id",
        ),
        UniqueConstraint(
            "school_id",
            "action_code",
            "actor_context_key",
            "resource_type",
            "resource_id",
            "idempotency_key_hash",
            name="uq_command_idempotency_records_scope_key",
        ),
        Index(
            "ix_command_idempotency_records_lookup",
            "school_id",
            "action_code",
            "actor_context_key",
            "resource_type",
            "resource_id",
            "idempotency_key_hash",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("schools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    action_code: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    actor_context_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_context_key: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_assignment_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    actor_assignment_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_guardian_relationship_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    actor_guardian_relationship_version: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="in_progress")
    result_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
