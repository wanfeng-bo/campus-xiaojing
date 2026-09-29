from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Text, and_, case, cast, desc, exists, func, literal, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import TableValuedAlias

from app.platform.contracts import (
    AssignmentId,
    EffectiveInterval,
    LocationId,
    PersonId,
    ResponsibilityGroupId,
    ResponsibilityTypeId,
    SchoolId,
    SemanticCode,
)
from app.platform.organization.models import StaffAssignment, StaffAssignmentVersion
from app.platform.responsibility.contracts import (
    LockedResponsibilityGroupFact,
    NewConfirmedResponsibilityMembership,
    RepairResponsibilityAssignmentFact,
    RepairResponsibilityGrantFact,
    RepairResponsibilityGroupFact,
    RepairResponsibilityMemberFact,
    ResponsibilityGroupSnapshot,
)
from app.platform.responsibility.models import (
    RepairResponsibilityGrant,
    ResponsibilityGroup,
    ResponsibilityGroupMembership,
    ResponsibilityGroupVersion,
    ResponsibilityType,
)


class SqlAlchemyResponsibilityRepository:
    """在调用方事务内写入并解析有限的报修责任事实。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _add_and_flush(self, model: object) -> None:
        self._session.add(model)
        self._session.flush()

    def add_type(self, responsibility_type: ResponsibilityType) -> None:
        self._add_and_flush(responsibility_type)

    def add_group(self, group: ResponsibilityGroup) -> None:
        self._add_and_flush(group)

    def add_group_version(self, version: ResponsibilityGroupVersion) -> None:
        self._add_and_flush(version)

    def add_membership(self, membership: ResponsibilityGroupMembership) -> None:
        self._add_and_flush(membership)

    def add_repair_grant(self, grant: RepairResponsibilityGrant) -> None:
        self._add_and_flush(grant)

    def get_group_for_update(
        self,
        school_id: SchoolId,
        group_id: ResponsibilityGroupId,
    ) -> ResponsibilityGroup | None:
        statement = (
            select(ResponsibilityGroup)
            .where(
                ResponsibilityGroup.school_id == school_id,
                ResponsibilityGroup.id == group_id,
            )
            .with_for_update()
        )
        return self._session.scalar(statement)

    def lock_group(
        self,
        school_id: SchoolId,
        group_id: ResponsibilityGroupId,
    ) -> LockedResponsibilityGroupFact | None:
        """锁定责任组稳定行并返回成员确认所需的最小事实。"""

        group = self.get_group_for_update(school_id, group_id)
        if group is None:
            return None
        return LockedResponsibilityGroupFact(
            school_id=SchoolId(group.school_id),
            responsibility_group_id=ResponsibilityGroupId(group.id),
            manager_assignment_id=AssignmentId(group.manager_assignment_id),
            manager_assignment_version=group.manager_assignment_version,
            status=group.status,  # type: ignore[arg-type]
            version=group.version,
        )

    def add_confirmed_membership(
        self,
        membership: NewConfirmedResponsibilityMembership,
    ) -> None:
        """加入已确认责任组成员并立即 flush。"""

        self.add_membership(
            ResponsibilityGroupMembership(
                id=membership.membership_id,
                school_id=membership.school_id,
                responsibility_group_id=membership.responsibility_group_id,
                assignment_id=membership.assignment_id,
                valid_from=membership.interval.valid_from,
                valid_to=membership.interval.valid_to,
                status="confirmed",
                version=1,
            )
        )

    def confirmed_membership_matches(
        self,
        membership: NewConfirmedResponsibilityMembership,
    ) -> bool:
        """逐字段核对固定 membership ID，不把缓存当权威事实。"""
        return SqlAlchemyConfirmedResponsibilityMembershipReader(
            self._session
        ).confirmed_membership_matches(membership)

    def get_repair_route_groups(
        self,
        school_id: SchoolId,
        group_ids: tuple[ResponsibilityGroupId, ...],
    ) -> tuple[RepairResponsibilityGroupFact, ...]:
        """按稳定 ID 读取报修路由所需的责任组状态。"""

        return self._repair_route_groups(school_id, group_ids, for_update=False)

    def lock_repair_route_groups(
        self,
        school_id: SchoolId,
        group_ids: tuple[ResponsibilityGroupId, ...],
    ) -> tuple[RepairResponsibilityGroupFact, ...]:
        """按稳定 UUID 序锁定责任组，不锁其他 owner 的表。"""

        return self._repair_route_groups(school_id, group_ids, for_update=True)

    def _repair_route_groups(
        self,
        school_id: SchoolId,
        group_ids: tuple[ResponsibilityGroupId, ...],
        *,
        for_update: bool,
    ) -> tuple[RepairResponsibilityGroupFact, ...]:
        if not group_ids:
            return ()
        statement = (
            select(ResponsibilityGroup)
            .where(
                ResponsibilityGroup.school_id == school_id,
                ResponsibilityGroup.id.in_(group_ids),
            )
            .order_by(ResponsibilityGroup.id)
        )
        if for_update:
            statement = statement.with_for_update(of=ResponsibilityGroup)
        return tuple(
            RepairResponsibilityGroupFact(
                school_id=SchoolId(group.school_id),
                responsibility_group_id=ResponsibilityGroupId(group.id),
                status=group.status,  # type: ignore[arg-type]
                version=group.version,
            )
            for group in self._session.scalars(statement)
        )

    def list_current_repair_route_members(
        self,
        school_id: SchoolId,
        group_id: ResponsibilityGroupId,
        *,
        as_of: datetime,
    ) -> tuple[RepairResponsibilityMemberFact, ...]:
        """读取当前有效成员及其精确任职，不依赖显示职称。"""

        return self._repair_route_members(
            school_id,
            group_id=group_id,
            membership_ids=None,
            as_of=as_of,
            for_update=False,
        )

    def get_current_repair_assignment(
        self,
        school_id: SchoolId,
        person_id: PersonId,
        assignment_id: AssignmentId,
        assignment_version: int,
        *,
        as_of: datetime,
    ) -> RepairResponsibilityAssignmentFact | None:
        """以同校、同人、同版本和有效期证明一条当前任职。"""

        row = self._session.execute(
            select(StaffAssignment, StaffAssignmentVersion)
            .join(
                StaffAssignmentVersion,
                (StaffAssignmentVersion.school_id == StaffAssignment.school_id)
                & (StaffAssignmentVersion.assignment_id == StaffAssignment.id),
            )
            .where(
                StaffAssignment.school_id == school_id,
                StaffAssignment.id == assignment_id,
                StaffAssignment.version == assignment_version,
                StaffAssignmentVersion.person_id == person_id,
                *_current_assignment_version_conditions(as_of),
            )
        ).one_or_none()
        if row is None:
            return None
        assignment, assignment_version_row = row
        return RepairResponsibilityAssignmentFact(
            school_id=SchoolId(assignment.school_id),
            person_id=PersonId(assignment_version_row.person_id),
            assignment_id=AssignmentId(assignment.id),
            assignment_version=assignment.version,
        )

    def list_current_repair_relationships_for_assignment(
        self,
        school_id: SchoolId,
        person_id: PersonId,
        assignment_id: AssignmentId,
        assignment_version: int,
        *,
        as_of: datetime,
    ) -> tuple[RepairResponsibilityMemberFact, ...]:
        """只返回任职、责任组、成员关系同时当前有效的服务端事实。"""

        statement = (
            select(ResponsibilityGroupMembership, StaffAssignment, StaffAssignmentVersion)
            .join(
                StaffAssignment,
                (StaffAssignment.school_id == ResponsibilityGroupMembership.school_id)
                & (StaffAssignment.id == ResponsibilityGroupMembership.assignment_id),
            )
            .join(
                StaffAssignmentVersion,
                (StaffAssignmentVersion.school_id == StaffAssignment.school_id)
                & (StaffAssignmentVersion.assignment_id == StaffAssignment.id),
            )
            .join(
                ResponsibilityGroup,
                (ResponsibilityGroup.school_id == ResponsibilityGroupMembership.school_id)
                & (ResponsibilityGroup.id == ResponsibilityGroupMembership.responsibility_group_id),
            )
            .where(
                ResponsibilityGroupMembership.school_id == school_id,
                ResponsibilityGroupMembership.assignment_id == assignment_id,
                ResponsibilityGroupMembership.status == "confirmed",
                ResponsibilityGroupMembership.valid_from <= as_of,
                or_(
                    ResponsibilityGroupMembership.valid_to.is_(None),
                    ResponsibilityGroupMembership.valid_to > as_of,
                ),
                ResponsibilityGroup.status == "active",
                StaffAssignmentVersion.person_id == person_id,
                StaffAssignment.version == assignment_version,
                *_current_assignment_version_conditions(as_of),
            )
            .order_by(ResponsibilityGroupMembership.id)
        )
        return tuple(
            RepairResponsibilityMemberFact(
                membership_id=membership.id,
                school_id=SchoolId(membership.school_id),
                responsibility_group_id=ResponsibilityGroupId(membership.responsibility_group_id),
                membership_version=membership.version,
                person_id=PersonId(assignment_version.person_id),
                assignment_id=AssignmentId(assignment.id),
                assignment_version=assignment.version,
            )
            for membership, assignment, assignment_version in self._session.execute(statement)
        )

    def lock_repair_route_memberships(
        self,
        school_id: SchoolId,
        membership_ids: tuple[UUID, ...],
        *,
        as_of: datetime,
    ) -> tuple[RepairResponsibilityMemberFact, ...]:
        """在候选任职已先锁定后，按稳定 UUID 序锁定成员关系。"""

        return self._repair_route_members(
            school_id,
            group_id=None,
            membership_ids=membership_ids,
            as_of=as_of,
            for_update=True,
        )

    def _repair_route_members(
        self,
        school_id: SchoolId,
        *,
        group_id: ResponsibilityGroupId | None,
        membership_ids: tuple[UUID, ...] | None,
        as_of: datetime,
        for_update: bool,
    ) -> tuple[RepairResponsibilityMemberFact, ...]:
        if membership_ids == ():
            return ()
        statement = (
            select(ResponsibilityGroupMembership, StaffAssignment, StaffAssignmentVersion)
            .join(
                StaffAssignment,
                (StaffAssignment.school_id == ResponsibilityGroupMembership.school_id)
                & (StaffAssignment.id == ResponsibilityGroupMembership.assignment_id),
            )
            .join(
                StaffAssignmentVersion,
                (StaffAssignmentVersion.school_id == StaffAssignment.school_id)
                & (StaffAssignmentVersion.assignment_id == StaffAssignment.id),
            )
            .where(
                ResponsibilityGroupMembership.school_id == school_id,
                ResponsibilityGroupMembership.status == "confirmed",
                ResponsibilityGroupMembership.valid_from <= as_of,
                or_(
                    ResponsibilityGroupMembership.valid_to.is_(None),
                    ResponsibilityGroupMembership.valid_to > as_of,
                ),
                *_current_assignment_version_conditions(as_of),
            )
            .order_by(ResponsibilityGroupMembership.id)
        )
        if group_id is not None:
            statement = statement.where(
                ResponsibilityGroupMembership.responsibility_group_id == group_id
            )
        if membership_ids is not None:
            statement = statement.where(ResponsibilityGroupMembership.id.in_(membership_ids))
        if for_update:
            statement = statement.with_for_update(of=ResponsibilityGroupMembership)
        return tuple(
            RepairResponsibilityMemberFact(
                membership_id=membership.id,
                school_id=SchoolId(membership.school_id),
                responsibility_group_id=ResponsibilityGroupId(membership.responsibility_group_id),
                membership_version=membership.version,
                person_id=PersonId(assignment_version.person_id),
                assignment_id=AssignmentId(assignment.id),
                assignment_version=assignment.version,
            )
            for membership, assignment, assignment_version in self._session.execute(statement)
        )

    def resolve_group_snapshot(
        self,
        school_id: SchoolId,
        responsibility_group_id: ResponsibilityGroupId,
        *,
        as_of: datetime,
    ) -> ResponsibilityGroupSnapshot | None:
        statement = (
            select(ResponsibilityGroupVersion)
            .join(
                ResponsibilityGroup,
                (ResponsibilityGroup.school_id == ResponsibilityGroupVersion.school_id)
                & (ResponsibilityGroup.id == ResponsibilityGroupVersion.responsibility_group_id),
            )
            .where(
                ResponsibilityGroupVersion.school_id == school_id,
                ResponsibilityGroupVersion.responsibility_group_id == responsibility_group_id,
                ResponsibilityGroupVersion.status == "confirmed",
                ResponsibilityGroupVersion.valid_from <= as_of,
                or_(
                    ResponsibilityGroupVersion.valid_to.is_(None),
                    ResponsibilityGroupVersion.valid_to > as_of,
                ),
            )
        )
        version = self._session.scalar(statement)
        if version is None:
            return None
        return ResponsibilityGroupSnapshot(
            school_id=SchoolId(version.school_id),
            responsibility_group_id=ResponsibilityGroupId(version.responsibility_group_id),
            display_name=version.display_name,
            source_version=version.version,
            captured_at=datetime.now(UTC),
            effective_interval=EffectiveInterval(
                valid_from=version.valid_from,
                valid_to=version.valid_to,
            ),
            snapshot_basis="responsibility.group_version_as_of",
        )

    def resolve_repair_grant(
        self,
        school_id: SchoolId,
        responsibility_type_id: ResponsibilityTypeId,
        *,
        location_id: LocationId | None,
        repair_subtype: SemanticCode | None,
        severity: SemanticCode | None,
        as_of: datetime,
    ) -> RepairResponsibilityGrantFact | None:
        specificity = (
            case((RepairResponsibilityGrant.location_id.is_not(None), 1), else_=0)
            + case((RepairResponsibilityGrant.repair_subtype.is_not(None), 1), else_=0)
            + case((RepairResponsibilityGrant.severity.is_not(None), 1), else_=0)
        )
        statement = (
            select(
                RepairResponsibilityGrant,
                ResponsibilityGroup,
                StaffAssignment,
                StaffAssignmentVersion,
            )
            .join(
                ResponsibilityType,
                (ResponsibilityType.school_id == RepairResponsibilityGrant.school_id)
                & (ResponsibilityType.id == RepairResponsibilityGrant.responsibility_type_id),
            )
            .join(
                ResponsibilityGroup,
                (ResponsibilityGroup.school_id == RepairResponsibilityGrant.school_id)
                & (ResponsibilityGroup.id == RepairResponsibilityGrant.responsibility_group_id),
            )
            .outerjoin(
                StaffAssignment,
                (StaffAssignment.school_id == ResponsibilityGroup.school_id)
                & (StaffAssignment.id == ResponsibilityGroup.manager_assignment_id)
                & (StaffAssignment.version == ResponsibilityGroup.manager_assignment_version),
            )
            .outerjoin(
                StaffAssignmentVersion,
                (StaffAssignmentVersion.school_id == StaffAssignment.school_id)
                & (StaffAssignmentVersion.assignment_id == StaffAssignment.id)
                & _current_assignment_version_expression(as_of),
            )
            .where(
                RepairResponsibilityGrant.school_id == school_id,
                RepairResponsibilityGrant.responsibility_type_id == responsibility_type_id,
                RepairResponsibilityGrant.status == "confirmed",
                ResponsibilityType.status == "active",
                RepairResponsibilityGrant.valid_from <= as_of,
                or_(
                    RepairResponsibilityGrant.valid_to.is_(None),
                    RepairResponsibilityGrant.valid_to > as_of,
                ),
                or_(
                    RepairResponsibilityGrant.location_id.is_(None),
                    RepairResponsibilityGrant.location_id == location_id,
                ),
                or_(
                    RepairResponsibilityGrant.repair_subtype.is_(None),
                    RepairResponsibilityGrant.repair_subtype == repair_subtype,
                ),
                or_(
                    RepairResponsibilityGrant.severity.is_(None),
                    RepairResponsibilityGrant.severity == severity,
                ),
            )
            .order_by(desc(specificity), RepairResponsibilityGrant.id)
            .limit(2)
        )
        matches = list(self._session.execute(statement))
        if not matches:
            return None
        if len(matches) > 1:
            first_score = sum(
                value is not None
                for value in (
                    matches[0][0].location_id,
                    matches[0][0].repair_subtype,
                    matches[0][0].severity,
                )
            )
            second_score = sum(
                value is not None
                for value in (
                    matches[1][0].location_id,
                    matches[1][0].repair_subtype,
                    matches[1][0].severity,
                )
            )
            if first_score == second_score:
                return None
        grant, group, manager_assignment, manager_assignment_version = matches[0]
        if (
            group.status != "active"
            or manager_assignment is None
            or manager_assignment_version is None
            or group.manager_assignment_id != manager_assignment.id
            or group.manager_assignment_version != manager_assignment.version
        ):
            return None
        return RepairResponsibilityGrantFact(
            school_id=SchoolId(grant.school_id),
            responsibility_type_id=ResponsibilityTypeId(grant.responsibility_type_id),
            responsibility_group_id=ResponsibilityGroupId(grant.responsibility_group_id),
            manager_assignment_id=AssignmentId(manager_assignment.id),
            manager_assignment_version=manager_assignment.version,
            location_id=LocationId(grant.location_id) if grant.location_id is not None else None,
            repair_subtype=grant.repair_subtype,
            severity=grant.severity,
            route_mode=grant.route_mode,  # type: ignore[arg-type]
            source_version=grant.version,
            captured_at=datetime.now(UTC),
            effective_interval=EffectiveInterval(
                valid_from=grant.valid_from,
                valid_to=grant.valid_to,
            ),
            snapshot_basis="responsibility.repair_grant_as_of",
        )


def _assignment_version_terminal() -> TableValuedAlias:
    """Use the narrow ledger function without granting responsibility broad ledger reads."""

    return func.app.school_fact_content_version_terminal(
        StaffAssignmentVersion.school_id,
        cast(literal("assignment"), Text),
        StaffAssignmentVersion.assignment_id,
        StaffAssignmentVersion.target_version,
    ).table_valued("operation", "effective_at", joins_implicitly=True)


def _current_assignment_version_conditions(as_of: Any) -> tuple[Any, ...]:
    """Return the authoritative assignment-content predicates for one query time."""

    terminal = _assignment_version_terminal()
    terminal_invalid = exists(
        select(1)
        .select_from(terminal)
        .where(
            or_(
                terminal.c.operation == "revoke",
                terminal.c.effective_at.is_(None),
                terminal.c.effective_at <= as_of,
            )
        )
    ).correlate(StaffAssignmentVersion)
    return (
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
        ~terminal_invalid,
        func.app.school_fact_root_is_effective_at(
            StaffAssignmentVersion.school_id,
            cast(literal("person"), Text),
            StaffAssignmentVersion.person_id,
            as_of,
        ),
        func.app.school_fact_root_is_effective_at(
            StaffAssignmentVersion.school_id,
            cast(literal("organization"), Text),
            StaffAssignmentVersion.organization_unit_id,
            as_of,
        ),
    )


def _current_assignment_version_expression(as_of: Any) -> Any:
    """Combine current content predicates for an outer-join ON clause."""

    return and_(*_current_assignment_version_conditions(as_of))


class SqlAlchemyConfirmedResponsibilityMembershipReader:
    """提交结果未知时只暴露固定成员行核验，不暴露成员写口。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def confirmed_membership_matches(
        self,
        membership: NewConfirmedResponsibilityMembership,
    ) -> bool:
        """逐字段核对固定成员 ID，不获取锁或返回额外业务字段。"""
        statement = select(ResponsibilityGroupMembership.id).where(
            ResponsibilityGroupMembership.id == membership.membership_id,
            ResponsibilityGroupMembership.school_id == membership.school_id,
            ResponsibilityGroupMembership.responsibility_group_id
            == membership.responsibility_group_id,
            ResponsibilityGroupMembership.assignment_id == membership.assignment_id,
            ResponsibilityGroupMembership.valid_from == membership.interval.valid_from,
            ResponsibilityGroupMembership.valid_to.is_(None)
            if membership.interval.valid_to is None
            else ResponsibilityGroupMembership.valid_to == membership.interval.valid_to,
            ResponsibilityGroupMembership.status == "confirmed",
            ResponsibilityGroupMembership.version == 1,
        )
        return self._session.scalar(statement) is not None
