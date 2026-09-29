from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_POSITIVE_VERSION = "current_version >= 1"
_VERSION_ROW_POSITIVE = "version >= 1"
_NONEMPTY_CODE = "btrim({column}) <> ''"
_SHA256 = "{column} ~ '^[0-9a-f]{{64}}$'"
_OPTIONAL_ASSIGNMENT_VERSION = (
    "(actor_assignment_id IS NULL AND actor_assignment_version IS NULL) OR "
    "(actor_assignment_id IS NOT NULL AND actor_assignment_version >= 1)"
)


class GovernanceDuty(Base):
    """Governance-owned stable duty identity; production starts with no rows."""

    __tablename__ = "governance_duties"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_governance_duties_school_id_id"),
        UniqueConstraint("school_id", "duty_code", name="uq_governance_duties_school_duty_code"),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="duty_code"),
            name="ck_governance_duties_code_nonempty",
        ),
        CheckConstraint(_POSITIVE_VERSION, name="ck_governance_duties_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("schools.id", ondelete="RESTRICT"), nullable=False
    )
    duty_code: Mapped[str] = mapped_column(String(120), nullable=False)
    current_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GovernanceDutyVersion(Base):
    """Append-only complete duty content selected by version and effective instant."""

    __tablename__ = "governance_duty_versions"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "duty_id",
            "version",
            name="uq_governance_duty_versions_target_version",
        ),
        ForeignKeyConstraint(
            ["school_id", "duty_id"],
            ["governance_duties.school_id", "governance_duties.id"],
            name="fk_governance_duty_versions_duty",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="display_name"),
            name="ck_governance_duty_versions_name_nonempty",
        ),
        CheckConstraint(
            _VERSION_ROW_POSITIVE,
            name="ck_governance_duty_versions_version_positive",
        ),
        Index(
            "ix_governance_duty_versions_as_of",
            "school_id",
            "duty_id",
            "effective_from",
            "version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    duty_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GovernanceScope(Base):
    """Stable Scope identity independent of any unresolved target-type vocabulary."""

    __tablename__ = "governance_scopes"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_governance_scopes_school_id_id"),
        UniqueConstraint("school_id", "scope_key", name="uq_governance_scopes_school_scope_key"),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="scope_key"),
            name="ck_governance_scopes_key_nonempty",
        ),
        CheckConstraint(_POSITIVE_VERSION, name="ck_governance_scopes_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("schools.id", ondelete="RESTRICT"), nullable=False
    )
    scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    current_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GovernanceScopeVersion(Base):
    """Append-only typed Scope document tied to an owner-defined schema version."""

    __tablename__ = "governance_scope_versions"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "scope_id",
            "version",
            name="uq_governance_scope_versions_target_version",
        ),
        ForeignKeyConstraint(
            ["school_id", "scope_id"],
            ["governance_scopes.school_id", "governance_scopes.id"],
            name="fk_governance_scope_versions_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="schema_owner"),
            name="ck_governance_scope_versions_owner_nonempty",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="schema_name"),
            name="ck_governance_scope_versions_schema_nonempty",
        ),
        CheckConstraint(
            "schema_version >= 1",
            name="ck_governance_scope_versions_schema_version_positive",
        ),
        CheckConstraint(
            "jsonb_typeof(reference_document) = 'object'",
            name="ck_governance_scope_versions_document_object",
        ),
        CheckConstraint(
            _SHA256.format(column="reference_sha256"),
            name="ck_governance_scope_versions_sha256",
        ),
        CheckConstraint(
            _VERSION_ROW_POSITIVE,
            name="ck_governance_scope_versions_version_positive",
        ),
        Index(
            "ix_governance_scope_versions_as_of",
            "school_id",
            "scope_id",
            "effective_from",
            "version",
        ),
        Index(
            "ix_governance_scope_versions_schema",
            "schema_owner",
            "schema_name",
            "schema_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    scope_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    schema_owner: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_name: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reference_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GovernanceGrant(Base):
    """Stable Grant identity with immutable assignment and duty bindings."""

    __tablename__ = "governance_grants"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_governance_grants_school_id_id"),
        UniqueConstraint(
            "school_id",
            "id",
            "duty_id",
            name="uq_governance_grants_school_id_duty",
        ),
        ForeignKeyConstraint(
            ["school_id", "assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_governance_grants_assignment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "duty_id"],
            ["governance_duties.school_id", "governance_duties.id"],
            name="fk_governance_grants_duty",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_POSITIVE_VERSION, name="ck_governance_grants_version_positive"),
        Index(
            "ix_governance_grants_assignment",
            "school_id",
            "assignment_id",
            "duty_id",
            "current_version",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    assignment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    duty_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    current_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GovernanceGrantVersion(Base):
    """Append-only Grant content/history including prospective terminal transitions."""

    __tablename__ = "governance_grant_versions"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "grant_id",
            "version",
            name="uq_governance_grant_versions_target_version",
        ),
        ForeignKeyConstraint(
            ["school_id", "grant_id", "duty_id"],
            [
                "governance_grants.school_id",
                "governance_grants.id",
                "governance_grants.duty_id",
            ],
            name="fk_governance_grant_versions_grant_duty",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "duty_id", "duty_version"],
            [
                "governance_duty_versions.school_id",
                "governance_duty_versions.duty_id",
                "governance_duty_versions.version",
            ],
            name="fk_governance_grant_versions_duty_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "scope_id", "scope_version"],
            [
                "governance_scope_versions.school_id",
                "governance_scope_versions.scope_id",
                "governance_scope_versions.version",
            ],
            name="fk_governance_grant_versions_scope_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "transition_kind IN ('create', 'update', 'end', 'revoke')",
            name="ck_governance_grant_versions_transition",
        ),
        CheckConstraint(
            "effective_from >= valid_from",
            name="ck_governance_grant_versions_effective_order",
        ),
        CheckConstraint(
            "planned_valid_to IS NULL OR planned_valid_to > valid_from",
            name="ck_governance_grant_versions_planned_interval",
        ),
        CheckConstraint(
            "(transition_kind IN ('create', 'update') "
            "AND formal_ended_at IS NULL AND revoked_at IS NULL) OR "
            "(transition_kind = 'end' AND formal_ended_at = effective_from "
            "AND revoked_at IS NULL) OR "
            "(transition_kind = 'revoke' AND revoked_at = effective_from "
            "AND formal_ended_at IS NULL)",
            name="ck_governance_grant_versions_terminal_shape",
        ),
        CheckConstraint(
            "(transition_kind = 'create' AND version = 1) OR "
            "(transition_kind <> 'create' AND version > 1)",
            name="ck_governance_grant_versions_create_version",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="basis_authority"),
            name="ck_governance_grant_versions_basis_authority",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="basis_reference"),
            name="ck_governance_grant_versions_basis_reference",
        ),
        CheckConstraint(
            "duty_version >= 1 AND scope_version >= 1 AND version >= 1",
            name="ck_governance_grant_versions_positive_versions",
        ),
        Index(
            "ix_governance_grant_versions_as_of",
            "school_id",
            "grant_id",
            "effective_from",
            "version",
        ),
        Index(
            "ix_governance_grant_versions_scope",
            "school_id",
            "scope_id",
            "scope_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    grant_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    duty_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    duty_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scope_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    scope_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    basis_authority: Mapped[str] = mapped_column(String(120), nullable=False)
    basis_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planned_valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    formal_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    transition_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GovernanceAssignmentEpoch(Base):
    """Persistent invalidation epoch for all Grants bound to one assignment."""

    __tablename__ = "governance_assignment_epochs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_governance_assignment_epochs_assignment",
            ondelete="RESTRICT",
        ),
        CheckConstraint("epoch >= 1", name="ck_governance_assignment_epochs_positive"),
    )

    school_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    assignment_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GovernanceBootstrapState(Base):
    """Persistent, explicitly closeable state for the external bootstrap authority."""

    __tablename__ = "governance_bootstrap_states"
    __table_args__ = (
        CheckConstraint(
            "state IN ('open', 'closed')",
            name="ck_governance_bootstrap_states_state",
        ),
        CheckConstraint(
            "(state = 'open' AND closed_at IS NULL) OR "
            "(state = 'closed' AND closed_at IS NOT NULL)",
            name="ck_governance_bootstrap_states_closed_shape",
        ),
        CheckConstraint(
            "closed_at IS NULL OR closed_at >= opened_at",
            name="ck_governance_bootstrap_states_closed_interval",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="authority_reference"),
            name="ck_governance_bootstrap_states_authority_reference",
        ),
        CheckConstraint("version >= 1", name="ck_governance_bootstrap_states_version_positive"),
    )

    school_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("schools.id", ondelete="RESTRICT"), primary_key=True
    )
    authority_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GovernanceCommandReceipt(Base):
    """Governance-owned command idempotency and commit-outcome authority."""

    __tablename__ = "governance_command_receipts"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "idempotency_key_hash",
            name="uq_governance_command_receipts_school_key",
        ),
        UniqueConstraint("school_id", "id", name="uq_governance_command_receipts_school_id_id"),
        ForeignKeyConstraint(
            ["school_id", "actor_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_governance_command_receipts_actor_assignment",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            _SHA256.format(column="idempotency_key_hash"),
            name="ck_governance_command_receipts_key_hash",
        ),
        CheckConstraint(
            _SHA256.format(column="request_sha256"),
            name="ck_governance_command_receipts_request_hash",
        ),
        CheckConstraint(
            "status IN ('started', 'succeeded')",
            name="ck_governance_command_receipts_status",
        ),
        CheckConstraint(
            _OPTIONAL_ASSIGNMENT_VERSION,
            name="ck_governance_command_receipts_actor_assignment",
        ),
        CheckConstraint(
            "(target_id IS NULL AND target_version IS NULL) OR "
            "(target_id IS NOT NULL AND target_version >= 1)",
            name="ck_governance_command_receipts_target",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="command_name"),
            name="ck_governance_command_receipts_command",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="actor_kind"),
            name="ck_governance_command_receipts_actor_kind",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="actor_reference"),
            name="ck_governance_command_receipts_actor_reference",
        ),
        Index(
            "ix_governance_command_receipts_request",
            "school_id",
            "request_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("schools.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    command_name: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_assignment_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    actor_assignment_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    target_kind: Mapped[str | None] = mapped_column(String(80), nullable=True)
    target_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    target_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    result_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GovernanceAuditEvent(Base):
    """Append-only typed Governance audit event linked one-to-one to a command."""

    __tablename__ = "governance_audit_events"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "command_receipt_id",
            name="uq_governance_audit_events_command",
        ),
        ForeignKeyConstraint(
            ["school_id", "command_receipt_id"],
            ["governance_command_receipts.school_id", "governance_command_receipts.id"],
            name="fk_governance_audit_events_command",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "actor_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_governance_audit_events_actor_assignment",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            _OPTIONAL_ASSIGNMENT_VERSION,
            name="ck_governance_audit_events_actor_assignment",
        ),
        CheckConstraint(
            "target_version >= 1",
            name="ck_governance_audit_events_target_version",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="event_type"),
            name="ck_governance_audit_events_event_type",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="target_kind"),
            name="ck_governance_audit_events_target_kind",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="actor_kind"),
            name="ck_governance_audit_events_actor_kind",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="actor_reference"),
            name="ck_governance_audit_events_actor_reference",
        ),
        CheckConstraint(
            _NONEMPTY_CODE.format(column="reason"),
            name="ck_governance_audit_events_reason",
        ),
        Index(
            "ix_governance_audit_events_target",
            "school_id",
            "target_kind",
            "target_id",
            "occurred_at",
        ),
        Index(
            "ix_governance_audit_events_request",
            "school_id",
            "request_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    command_receipt_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_assignment_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    actor_assignment_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    target_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    before_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    after_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
