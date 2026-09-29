from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Text, cast, exists, func, literal, or_, select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import TableValuedAlias

from app.platform.contracts import (
    AssignmentId,
    EffectiveInterval,
    OrganizationUnitId,
    PersonId,
    SchoolId,
)
from app.platform.organization.contracts import (
    AssignmentSnapshot,
    LockedOrganizationUnitFact,
    LockedStaffAssignmentFact,
    NewConfirmedStaffAssignment,
    OrganizationUnitSnapshot,
)
from app.platform.organization.models import (
    OrganizationUnit,
    OrganizationUnitVersion,
    StaffAssignment,
    StaffAssignmentVersion,
)


class SqlAlchemyOrganizationRepository:
    """在调用方事务内维护组织版本与正式任职。"""

    def __init__(self, session: Session, *, use_reader_identity_wrappers: bool = False) -> None:
        """绑定调用方 Session；受信任 Reader 组合可选择固定身份窄函数。"""

        self._session = session
        self._use_reader_identity_wrappers = use_reader_identity_wrappers

    def add_unit(self, unit: OrganizationUnit) -> None:
        """把组织稳定主体加入现有事务并 flush。"""

        self._session.add(unit)
        self._session.flush()

    def add_unit_version(self, version: OrganizationUnitVersion) -> None:
        """加入组织显示版本并 flush，使区间冲突立即暴露。"""

        self._session.add(version)
        self._session.flush()

    def add_assignment(self, assignment: StaffAssignment) -> None:
        """加入正式任职并 flush，使跨校或重叠错误立即暴露。"""

        self._session.add(assignment)
        self._session.flush()

    def get_assignment_for_update(
        self,
        school_id: SchoolId,
        assignment_id: AssignmentId,
    ) -> StaffAssignment | None:
        """在学校范围内锁定指定任职，不锁定无关关系。"""

        statement = (
            select(StaffAssignment)
            .where(
                StaffAssignment.school_id == school_id,
                StaffAssignment.id == assignment_id,
            )
            .with_for_update()
        )
        return self._session.scalar(statement)

    def save_assignment(self, assignment: StaffAssignment) -> None:
        """flush 任职变更，不执行 commit 或 rollback。"""

        self._session.add(assignment)
        self._session.flush()

    def lock_assignment(
        self,
        school_id: SchoolId,
        assignment_id: AssignmentId,
    ) -> LockedStaffAssignmentFact | None:
        """锁任职稳定行并返回授权重验所需的最小事实。"""

        terminal = _content_version_terminal(
            fact_kind="assignment",
            school_id=StaffAssignmentVersion.school_id,
            target_id=StaffAssignmentVersion.assignment_id,
            content_version=StaffAssignmentVersion.target_version,
        )
        statement = (
            select(StaffAssignment, StaffAssignmentVersion)
            .join(
                StaffAssignmentVersion,
                (StaffAssignmentVersion.school_id == StaffAssignment.school_id)
                & (StaffAssignmentVersion.assignment_id == StaffAssignment.id),
            )
            .where(
                StaffAssignment.school_id == school_id,
                StaffAssignment.id == assignment_id,
                StaffAssignmentVersion.status == "confirmed",
                StaffAssignmentVersion.revoked_at.is_(None),
                StaffAssignmentVersion.effective_from <= func.statement_timestamp(),
                or_(
                    StaffAssignmentVersion.effective_to.is_(None),
                    StaffAssignmentVersion.effective_to > func.statement_timestamp(),
                ),
                or_(
                    StaffAssignmentVersion.planned_valid_to.is_(None),
                    StaffAssignmentVersion.planned_valid_to > func.statement_timestamp(),
                ),
                ~_terminal_invalid_at(
                    terminal,
                    as_of=func.statement_timestamp(),
                    correlate_from=StaffAssignmentVersion,
                ),
            )
            .with_for_update(of=StaffAssignment)
        )
        row = self._session.execute(statement).one_or_none()
        if row is None:
            return None
        assignment, version = row
        return LockedStaffAssignmentFact(
            school_id=SchoolId(assignment.school_id),
            assignment_id=AssignmentId(assignment.id),
            person_id=PersonId(version.person_id),
            organization_unit_id=OrganizationUnitId(version.organization_unit_id),
            semantic_role_code=version.semantic_role_code,
            status=version.status,  # type: ignore[arg-type]
            version=assignment.version,
            interval=EffectiveInterval(
                valid_from=version.effective_from,
                valid_to=version.effective_to,
            ),
        )

    def lock_organization_unit(
        self,
        school_id: SchoolId,
        organization_unit_id: OrganizationUnitId,
        *,
        as_of: datetime,
    ) -> LockedOrganizationUnitFact | None:
        """锁定确认卡目标组织，以账本时态裁定当前有效性。"""

        statement = (
            select(
                OrganizationUnit,
                func.app.school_fact_root_is_effective_at(
                    OrganizationUnit.school_id,
                    cast(literal("organization"), Text),
                    OrganizationUnit.id,
                    as_of,
                ).label("is_effective"),
            )
            .where(
                OrganizationUnit.school_id == school_id,
                OrganizationUnit.id == organization_unit_id,
            )
            .with_for_update(of=OrganizationUnit)
        )
        row = self._session.execute(statement).one_or_none()
        if row is None or not row.is_effective:
            return None
        unit = row.OrganizationUnit
        return LockedOrganizationUnitFact(
            school_id=SchoolId(unit.school_id),
            organization_unit_id=OrganizationUnitId(unit.id),
            status="active",
            version=unit.version,
        )

    def add_confirmed_assignment(self, assignment: NewConfirmedStaffAssignment) -> None:
        """把已重验的正式任职加入当前事务并立即 flush。"""

        self.add_assignment(
            StaffAssignment(
                id=assignment.assignment_id,
                school_id=assignment.school_id,
                person_id=assignment.person_id,
                organization_unit_id=assignment.organization_unit_id,
                position_title=assignment.position_title,
                semantic_role_code=assignment.semantic_role_code,
                appointment_source="manual",
                approval_reference=assignment.approval_reference,
                valid_from=assignment.interval.valid_from,
                valid_to=assignment.interval.valid_to,
                status="confirmed",
                version=1,
            )
        )

    def confirmed_assignment_matches(
        self,
        assignment: NewConfirmedStaffAssignment,
    ) -> bool:
        """逐字段核对固定任职 ID，不用幂等缓存替代权威事实。"""
        return SqlAlchemyConfirmedStaffAssignmentReader(self._session).confirmed_assignment_matches(
            assignment
        )

    def get_current_assignment(
        self,
        school_id: SchoolId,
        assignment_id: AssignmentId,
        *,
        as_of: datetime,
    ) -> AssignmentSnapshot | None:
        """解析本模块当前任职；完整授权由上下文解析器组合其他 owner 校验。"""

        statement = self.current_assignment_statement(school_id, assignment_id, as_of=as_of)
        row = self._session.execute(statement).one_or_none()
        return self.assignment_snapshot_from_row(row)

    def current_assignment_statement(
        self, school_id: SchoolId, assignment_id: AssignmentId, *, as_of: datetime
    ):
        """Fixed owner relation, also used by the composed three-root authority read."""

        assignment_terminal = (
            func.app.school_fact_reader_assignment_terminal(
                StaffAssignmentVersion.school_id,
                StaffAssignmentVersion.assignment_id,
                StaffAssignmentVersion.target_version,
            ).table_valued("operation", "effective_at", joins_implicitly=True)
            if self._use_reader_identity_wrappers
            else _content_version_terminal(
                fact_kind="assignment",
                school_id=StaffAssignmentVersion.school_id,
                target_id=StaffAssignmentVersion.assignment_id,
                content_version=StaffAssignmentVersion.target_version,
            )
        )
        root_is_effective = (
            func.app.school_fact_reader_identity_root_is_effective_at
            if self._use_reader_identity_wrappers
            else func.app.school_fact_root_is_effective_at
        )
        statement = (
            select(
                StaffAssignment,
                StaffAssignmentVersion,
                OrganizationUnitVersion,
                func.clock_timestamp().label("captured_at"),
            )
            .join(
                StaffAssignmentVersion,
                (StaffAssignmentVersion.school_id == StaffAssignment.school_id)
                & (StaffAssignmentVersion.assignment_id == StaffAssignment.id),
            )
            .join(
                OrganizationUnitVersion,
                (OrganizationUnitVersion.school_id == StaffAssignmentVersion.school_id)
                & (
                    OrganizationUnitVersion.organization_unit_id
                    == StaffAssignmentVersion.organization_unit_id
                ),
            )
            .where(
                StaffAssignment.school_id == school_id,
                StaffAssignment.id == assignment_id,
                StaffAssignmentVersion.status == "confirmed",
                StaffAssignmentVersion.revoked_at.is_(None),
                StaffAssignmentVersion.effective_from <= as_of,
                or_(
                    StaffAssignmentVersion.effective_to.is_(None),
                    StaffAssignmentVersion.effective_to > as_of,
                ),
                or_(
                    StaffAssignmentVersion.planned_valid_to.is_(None),
                    StaffAssignmentVersion.planned_valid_to > as_of,
                ),
                ~_terminal_invalid_at(
                    assignment_terminal,
                    as_of=as_of,
                    correlate_from=StaffAssignmentVersion,
                ),
                root_is_effective(
                    StaffAssignmentVersion.school_id,
                    cast(literal("organization"), Text),
                    StaffAssignmentVersion.organization_unit_id,
                    as_of,
                ),
                OrganizationUnitVersion.status == "confirmed",
                OrganizationUnitVersion.valid_from <= as_of,
                or_(
                    OrganizationUnitVersion.valid_to.is_(None),
                    OrganizationUnitVersion.valid_to > as_of,
                ),
            )
        )
        return statement

    def resolve_organization_unit_snapshot(
        self,
        school_id: SchoolId,
        organization_unit_id: OrganizationUnitId,
        *,
        as_of: datetime,
    ) -> OrganizationUnitSnapshot | None:
        """按历史时点解析组织的显示内容和父级结构。"""

        terminal = _content_version_terminal(
            fact_kind="organization",
            school_id=OrganizationUnitVersion.school_id,
            target_id=OrganizationUnitVersion.organization_unit_id,
            content_version=OrganizationUnitVersion.version,
        )
        statement = select(
            OrganizationUnitVersion,
            func.clock_timestamp().label("captured_at"),
        ).where(
            OrganizationUnitVersion.school_id == school_id,
            OrganizationUnitVersion.organization_unit_id == organization_unit_id,
            OrganizationUnitVersion.status == "confirmed",
            OrganizationUnitVersion.valid_from <= as_of,
            or_(
                OrganizationUnitVersion.valid_to.is_(None),
                OrganizationUnitVersion.valid_to > as_of,
            ),
            ~_terminal_invalid_at(
                terminal,
                as_of=as_of,
                correlate_from=OrganizationUnitVersion,
            ),
        )
        row = self._session.execute(statement).one_or_none()
        if row is None:
            return None
        version, captured_at = row
        return OrganizationUnitSnapshot(
            school_id=SchoolId(version.school_id),
            organization_unit_id=OrganizationUnitId(version.organization_unit_id),
            parent_organization_unit_id=(
                OrganizationUnitId(version.parent_organization_unit_id)
                if version.parent_organization_unit_id is not None
                else None
            ),
            display_name=version.display_name,
            source_version=version.version,
            captured_at=captured_at,
            effective_interval=EffectiveInterval(
                valid_from=version.valid_from,
                valid_to=version.valid_to,
            ),
            snapshot_basis="organization.unit_version_as_of",
        )

    def resolve_assignment_snapshot(
        self,
        school_id: SchoolId,
        assignment_id: AssignmentId,
        *,
        as_of: datetime,
    ) -> AssignmentSnapshot | None:
        """按历史时点解析任职，并以正式账本排除撤销内容。"""

        assignment_terminal = _content_version_terminal(
            fact_kind="assignment",
            school_id=StaffAssignmentVersion.school_id,
            target_id=StaffAssignmentVersion.assignment_id,
            content_version=StaffAssignmentVersion.target_version,
        )
        organization_terminal = _content_version_terminal(
            fact_kind="organization",
            school_id=OrganizationUnitVersion.school_id,
            target_id=OrganizationUnitVersion.organization_unit_id,
            content_version=OrganizationUnitVersion.version,
        )
        statement = (
            select(
                StaffAssignment,
                StaffAssignmentVersion,
                OrganizationUnitVersion,
                func.clock_timestamp().label("captured_at"),
            )
            .join(
                StaffAssignmentVersion,
                (StaffAssignmentVersion.school_id == StaffAssignment.school_id)
                & (StaffAssignmentVersion.assignment_id == StaffAssignment.id),
            )
            .join(
                OrganizationUnitVersion,
                (OrganizationUnitVersion.school_id == StaffAssignmentVersion.school_id)
                & (
                    OrganizationUnitVersion.organization_unit_id
                    == StaffAssignmentVersion.organization_unit_id
                ),
            )
            .where(
                StaffAssignment.school_id == school_id,
                StaffAssignment.id == assignment_id,
                StaffAssignmentVersion.status == "confirmed",
                StaffAssignmentVersion.revoked_at.is_(None),
                StaffAssignmentVersion.effective_from <= as_of,
                or_(
                    StaffAssignmentVersion.effective_to.is_(None),
                    StaffAssignmentVersion.effective_to > as_of,
                ),
                or_(
                    StaffAssignmentVersion.planned_valid_to.is_(None),
                    StaffAssignmentVersion.planned_valid_to > as_of,
                ),
                OrganizationUnitVersion.status == "confirmed",
                OrganizationUnitVersion.valid_from <= as_of,
                or_(
                    OrganizationUnitVersion.valid_to.is_(None),
                    OrganizationUnitVersion.valid_to > as_of,
                ),
                ~_terminal_invalid_at(
                    assignment_terminal,
                    as_of=as_of,
                    correlate_from=StaffAssignmentVersion,
                ),
                ~_terminal_invalid_at(
                    organization_terminal,
                    as_of=as_of,
                    correlate_from=OrganizationUnitVersion,
                ),
            )
        )
        row = self._session.execute(statement).one_or_none()
        return self.assignment_snapshot_from_row(row)

    @staticmethod
    def assignment_snapshot_from_row(
        row: Row[tuple[StaffAssignment, StaffAssignmentVersion, OrganizationUnitVersion, datetime]]
        | None,
    ) -> AssignmentSnapshot | None:
        """把已筛选的任职与组织版本压平为不可变快照。"""

        if row is None:
            return None
        assignment, assignment_version, unit_version, captured_at = row
        return AssignmentSnapshot(
            school_id=SchoolId(assignment.school_id),
            assignment_id=AssignmentId(assignment.id),
            person_id=PersonId(assignment_version.person_id),
            organization_unit_id=OrganizationUnitId(assignment_version.organization_unit_id),
            organization_display_name=unit_version.display_name,
            position_title=assignment_version.position_title,
            semantic_role_code=assignment_version.semantic_role_code,
            assignment_kind=assignment_version.assignment_kind,
            appointment_source=assignment_version.appointment_source,
            approval_reference=assignment_version.approval_reference,
            planned_valid_to=assignment_version.planned_valid_to,
            status=assignment_version.status,  # type: ignore[arg-type]
            source_version=assignment_version.target_version,
            captured_at=captured_at,
            effective_interval=EffectiveInterval(
                valid_from=assignment_version.effective_from,
                valid_to=assignment_version.effective_to,
            ),
            snapshot_basis="organization.assignment_content_slice_as_of",
        )


def _content_version_terminal(
    *,
    fact_kind: str,
    school_id: Any,
    target_id: Any,
    content_version: Any,
) -> TableValuedAlias:
    """Build the narrow ledger lookup without granting runtime table access."""

    return func.app.school_fact_content_version_terminal(
        school_id,
        cast(literal(fact_kind), Text),
        target_id,
        content_version,
    ).table_valued("operation", "effective_at", joins_implicitly=True)


def _terminal_invalid_at(
    terminal: TableValuedAlias,
    *,
    as_of: Any,
    correlate_from: Any,
):
    """A revoke erases the slice; an end bounds it at its recorded effective time."""

    return exists(
        select(1)
        .select_from(terminal)
        .where(
            or_(
                terminal.c.operation == "revoke",
                terminal.c.effective_at.is_(None),
                terminal.c.effective_at <= as_of,
            )
        )
    ).correlate(correlate_from)


class SqlAlchemyConfirmedStaffAssignmentReader:
    """提交结果未知时只暴露固定任职行核验，不暴露任职写口。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def confirmed_assignment_matches(
        self,
        assignment: NewConfirmedStaffAssignment,
    ) -> bool:
        """逐字段核对固定任职 ID，不获取锁或返回额外业务字段。"""
        statement = select(StaffAssignment.id).where(
            StaffAssignment.id == assignment.assignment_id,
            StaffAssignment.school_id == assignment.school_id,
            StaffAssignment.person_id == assignment.person_id,
            StaffAssignment.organization_unit_id == assignment.organization_unit_id,
            StaffAssignment.position_title == assignment.position_title,
            StaffAssignment.semantic_role_code == assignment.semantic_role_code,
            StaffAssignment.appointment_source == "manual",
            StaffAssignment.approval_reference == assignment.approval_reference,
            StaffAssignment.valid_from == assignment.interval.valid_from,
            StaffAssignment.valid_to.is_(assignment.interval.valid_to)
            if assignment.interval.valid_to is None
            else StaffAssignment.valid_to == assignment.interval.valid_to,
            StaffAssignment.status == "confirmed",
            StaffAssignment.version == 1,
        )
        return self._session.scalar(statement) is not None
