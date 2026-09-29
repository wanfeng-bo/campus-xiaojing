from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    """以追加方式保存权限配置等关键操作的变更前后摘要。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_audit_logs_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "actor_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_audit_logs_school_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "actor_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_audit_logs_school_assignment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "actor_guardian_relationship_id"],
            ["guardian_relationships.school_id", "guardian_relationships.id"],
            name="fk_audit_logs_school_guardian",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
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
            name="ck_audit_logs_typed_business_context",
        ),
        CheckConstraint(
            "event_type IS DISTINCT FROM 'master_data.assignment.changed' "
            "OR target_type IS DISTINCT FROM 'staff_assignment' "
            "OR reason IS DISTINCT FROM 'manual.assignment-confirmed' "
            "OR (after_summary ->> 'state_code') = 'assignment.confirmed'",
            name="ck_audit_logs_staff_assignment_confirmed_transition",
        ),
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_actor_created_at", "actor_user_id", "created_at"),
        Index(
            "ix_audit_logs_target_created_at",
            "target_type",
            "target_id",
            "created_at",
        ),
        Index("ix_audit_logs_request_id", "request_id"),
        Index("ix_audit_logs_school_created_at", "school_id", "created_at"),
        Index(
            "uq_audit_logs_master_data_import_fact",
            "school_id",
            "idempotency_key_hash",
            "event_type",
            "target_type",
            "target_id",
            unique=True,
            postgresql_where=text(
                "event_type IN ('master_data.person.imported', "
                "'master_data.calendar.published', 'master_data.import.taken_over')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    school_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    actor_person_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    actor_assignment_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    actor_assignment_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_guardian_relationship_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    actor_guardian_relationship_version: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    actor_context_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    target_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_id: Mapped[str] = mapped_column(String(255), nullable=False)
    before_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    after_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
