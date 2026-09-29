from __future__ import annotations

from datetime import datetime
from secrets import token_urlsafe
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import TextClause

from app.db.base import Base


def _effective_range() -> TextClause:
    return text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')")


def new_repair_public_ref() -> str:
    """生成不包含内部 UUID、可安全展示给使用者的随机引用。"""

    return f"rep_{token_urlsafe(18)}"


class RepairScopeBinding(Base):
    """把报修处理或总务职责绑定到学校或一个精确地点范围。"""

    __tablename__ = "repair_scope_bindings"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "id",
            name="uq_repair_scope_bindings_school_id_id",
        ),
        ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_repair_scope_bindings_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_repair_scope_bindings_location",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "responsibility_group_id"],
            ["responsibility_groups.school_id", "responsibility_groups.id"],
            name="fk_repair_scope_bindings_group",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "purpose IN ('handler', 'general_affairs')",
            name="ck_repair_scope_bindings_purpose",
        ),
        CheckConstraint(
            "scope_kind IN ('school', 'location')",
            name="ck_repair_scope_bindings_scope_kind",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_repair_scope_bindings_status",
        ),
        CheckConstraint(
            "(scope_kind = 'school' AND location_id IS NULL) OR "
            "(scope_kind = 'location' AND location_id IS NOT NULL)",
            name="ck_repair_scope_bindings_location_scope",
        ),
        CheckConstraint(
            "purpose <> 'general_affairs' OR scope_kind = 'school'",
            name="ck_repair_scope_bindings_general_affairs_school",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_repair_scope_bindings_valid_interval",
        ),
        CheckConstraint(
            "responsibility_group_version >= 1",
            name="ck_repair_scope_bindings_group_version",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_repair_scope_bindings_version",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("purpose", "="),
            ("scope_kind", "="),
            (
                text("COALESCE(location_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
                "=",
            ),
            (_effective_range(), "&&"),
            where=text("status = 'active'"),
            using="gist",
            name="excl_repair_scope_bindings_active_scope_period",
        ),
        Index(
            "ix_repair_scope_bindings_current_scope",
            "school_id",
            "purpose",
            "scope_kind",
            "location_id",
            "status",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    location_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    responsibility_group_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    responsibility_group_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class RepairTicket(Base):
    """保存文字报修事实；个人关注和模型内容不进入这张表。"""

    __tablename__ = "repair_tickets"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_repair_tickets_school_id_id"),
        UniqueConstraint("public_ref", name="uq_repair_tickets_public_ref"),
        ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_repair_tickets_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "reporter_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_repair_tickets_reporter_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "reporter_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_repair_tickets_reporter_assignment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "recognized_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_repair_tickets_location",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "initial_binding_id"],
            ["repair_scope_bindings.school_id", "repair_scope_bindings.id"],
            name="fk_repair_tickets_initial_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "initial_group_id"],
            ["responsibility_groups.school_id", "responsibility_groups.id"],
            name="fk_repair_tickets_initial_group",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "designated_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_repair_tickets_designated_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "designated_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_repair_tickets_designated_assignment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "current_handler_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_repair_tickets_handler_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "current_handler_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_repair_tickets_handler_assignment",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision >= 1", name="ck_repair_tickets_revision"),
        CheckConstraint(
            "reporter_assignment_version >= 1",
            name="ck_repair_tickets_reporter_assignment_version",
        ),
        CheckConstraint(
            "btrim(reporter_name_snapshot) <> '' "
            "AND btrim(location_text) <> '' "
            "AND btrim(problem_description) <> ''",
            name="ck_repair_tickets_required_text",
        ),
        CheckConstraint(
            "reporter_phone_snapshot IS NULL OR btrim(reporter_phone_snapshot) <> ''",
            name="ck_repair_tickets_phone_nonblank",
        ),
        CheckConstraint(
            "result_code IS NULL OR btrim(result_code) <> ''",
            name="ck_repair_tickets_result_nonblank",
        ),
        CheckConstraint(
            "status IN ('submitted', 'processing', 'completed')",
            name="ck_repair_tickets_status",
        ),
        CheckConstraint(
            "initial_route_kind IN ('ordinary', 'general_affairs', 'unresolved')",
            name="ck_repair_tickets_initial_route_kind",
        ),
        CheckConstraint(
            "initial_resolution_branch IN ("
            "'location_handler', 'school_handler_after_location_zero', "
            "'school_handler_without_location', 'general_affairs', 'unresolved')",
            name="ck_repair_tickets_initial_resolution_branch",
        ),
        CheckConstraint(
            "(initial_route_kind = 'ordinary' AND initial_resolution_branch IN ("
            "'location_handler', 'school_handler_after_location_zero', "
            "'school_handler_without_location')) OR "
            "(initial_route_kind = 'general_affairs' "
            "AND initial_resolution_branch = 'general_affairs') OR "
            "(initial_route_kind = 'unresolved' "
            "AND initial_resolution_branch = 'unresolved')",
            name="ck_repair_tickets_initial_route_branch",
        ),
        CheckConstraint(
            "initial_candidate_count >= 0",
            name="ck_repair_tickets_initial_candidate_count",
        ),
        CheckConstraint(
            "(initial_route_kind = 'unresolved' "
            "AND initial_binding_id IS NULL "
            "AND initial_binding_version IS NULL "
            "AND initial_group_id IS NULL "
            "AND initial_group_version IS NULL "
            "AND initial_candidate_count = 0) OR "
            "(initial_route_kind IN ('ordinary', 'general_affairs') "
            "AND initial_binding_id IS NOT NULL "
            "AND initial_binding_version >= 1 "
            "AND initial_group_id IS NOT NULL "
            "AND initial_group_version >= 1 "
            "AND initial_candidate_count >= 1)",
            name="ck_repair_tickets_initial_route_proof",
        ),
        CheckConstraint(
            "(designated_person_id IS NULL "
            "AND designated_assignment_id IS NULL "
            "AND designated_assignment_version IS NULL) OR "
            "(designated_person_id IS NOT NULL "
            "AND designated_assignment_id IS NOT NULL "
            "AND designated_assignment_version >= 1)",
            name="ck_repair_tickets_designated_triplet",
        ),
        CheckConstraint(
            "(current_handler_person_id IS NULL "
            "AND current_handler_assignment_id IS NULL "
            "AND current_handler_assignment_version IS NULL) OR "
            "(current_handler_person_id IS NOT NULL "
            "AND current_handler_assignment_id IS NOT NULL "
            "AND current_handler_assignment_version >= 1)",
            name="ck_repair_tickets_current_handler_triplet",
        ),
        CheckConstraint(
            "(status = 'submitted' "
            "AND current_handler_person_id IS NULL "
            "AND started_at IS NULL "
            "AND completed_at IS NULL "
            "AND result_code IS NULL) OR "
            "(status = 'processing' "
            "AND current_handler_person_id IS NOT NULL "
            "AND started_at IS NOT NULL "
            "AND completed_at IS NULL "
            "AND result_code IS NULL) OR "
            "(status = 'completed' "
            "AND current_handler_person_id IS NOT NULL "
            "AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL "
            "AND result_code IS NOT NULL)",
            name="ck_repair_tickets_status_facts",
        ),
        CheckConstraint(
            "started_at IS NULL OR started_at >= submitted_at",
            name="ck_repair_tickets_start_after_submit",
        ),
        CheckConstraint(
            "completed_at IS NULL OR (started_at IS NOT NULL AND completed_at >= started_at)",
            name="ck_repair_tickets_completion_after_start",
        ),
        Index(
            "ix_repair_tickets_reporter",
            "school_id",
            "reporter_person_id",
            "submitted_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    public_ref: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=new_repair_public_ref,
    )
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )
    reporter_person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    reporter_assignment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    reporter_assignment_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reporter_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    reporter_phone_snapshot: Mapped[str | None] = mapped_column(String(32), nullable=True)
    location_text: Mapped[str] = mapped_column(String(200), nullable=False)
    recognized_location_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    problem_description: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    initial_route_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    initial_binding_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    initial_binding_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    initial_group_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    initial_group_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    initial_candidate_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    initial_resolution_branch: Mapped[str] = mapped_column(String(80), nullable=False)
    initial_resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    designated_person_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    designated_assignment_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    designated_assignment_version: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    current_handler_person_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    current_handler_assignment_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    current_handler_assignment_version: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
