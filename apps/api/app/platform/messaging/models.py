from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
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


class OutboxMessage(Base):
    """保存业务事务提交后才允许被 Worker 读取的安全事件。"""

    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint("school_id", "event_key", name="uq_outbox_messages_school_event_key"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'uncertain', 'dead_letter')",
            name="ck_outbox_messages_status",
        ),
        CheckConstraint("aggregate_version >= 1", name="ck_outbox_messages_version_positive"),
        CheckConstraint("attempt_count >= 0", name="ck_outbox_messages_attempt_count"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object' "
            "AND payload ?& ARRAY['event_type','aggregate_type','aggregate_id',"
            "'aggregate_version','occurred_at'] "
            "AND (payload - ARRAY['event_type','aggregate_type','aggregate_id',"
            "'aggregate_version','occurred_at']::text[]) = '{}'::jsonb "
            "AND jsonb_typeof(payload->'event_type') = 'string' "
            "AND jsonb_typeof(payload->'aggregate_type') = 'string' "
            "AND jsonb_typeof(payload->'aggregate_id') = 'string' "
            "AND jsonb_typeof(payload->'aggregate_version') = 'number' "
            "AND jsonb_typeof(payload->'occurred_at') = 'string' "
            "AND payload->>'event_type' = event_type "
            "AND payload->>'aggregate_type' = aggregate_type "
            "AND payload->>'aggregate_id' = aggregate_id::text "
            "AND (payload->>'aggregate_version')::bigint = aggregate_version "
            "AND (payload->>'occurred_at')::timestamptz = occurred_at",
            name="ck_outbox_messages_safe_payload",
        ),
        Index("ix_outbox_messages_pending", "status", "available_at", "id"),
        {"implicit_returning": False},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    school_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("schools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    aggregate_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
