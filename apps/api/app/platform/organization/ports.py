from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.platform.contracts import (
    AssignmentId,
    OrganizationUnitId,
    SchoolId,
)
from app.platform.organization.contracts import (
    AssignmentSnapshotResolver,
    CurrentAssignmentReader,
    LockedOrganizationUnitFact,
    LockedStaffAssignmentFact,
    NewConfirmedStaffAssignment,
)
from app.platform.organization.models import (
    OrganizationUnit,
    OrganizationUnitVersion,
    StaffAssignment,
)


class ConfirmedStaffAssignmentReader(Protocol):
    """只按固定 ID 与全部不可变字段核验已确认任职。"""

    def confirmed_assignment_matches(
        self,
        assignment: NewConfirmedStaffAssignment,
    ) -> bool: ...


class StaffAssignmentMutationPort(ConfirmedStaffAssignmentReader, Protocol):
    """供单一跨模块 Handler 使用的无提交任职窄写口。"""

    def lock_assignment(
        self,
        school_id: SchoolId,
        assignment_id: AssignmentId,
    ) -> LockedStaffAssignmentFact | None: ...

    def lock_organization_unit(
        self,
        school_id: SchoolId,
        organization_unit_id: OrganizationUnitId,
        *,
        as_of: datetime,
    ) -> LockedOrganizationUnitFact | None: ...

    def add_confirmed_assignment(self, assignment: NewConfirmedStaffAssignment) -> None: ...


class OrganizationRepository(
    CurrentAssignmentReader,
    AssignmentSnapshotResolver,
    StaffAssignmentMutationPort,
    Protocol,
):
    """定义组织版本和正式任职所需的最小写入边界。"""

    def add_unit(self, unit: OrganizationUnit) -> None:
        """加入组织稳定主体并 flush，不提交事务。"""

        ...

    def add_unit_version(self, version: OrganizationUnitVersion) -> None:
        """加入组织显示版本并触发有效期约束。"""

        ...

    def add_assignment(self, assignment: StaffAssignment) -> None:
        """加入正式任职并触发同校和岗位槽约束。"""

        ...

    def get_assignment_for_update(
        self,
        school_id: SchoolId,
        assignment_id: AssignmentId,
    ) -> StaffAssignment | None:
        """在学校范围内按任职 ID 获取行锁。"""

        ...

    def save_assignment(self, assignment: StaffAssignment) -> None:
        """flush 任职变更，由外层 UoW 决定提交或回滚。"""

        ...
