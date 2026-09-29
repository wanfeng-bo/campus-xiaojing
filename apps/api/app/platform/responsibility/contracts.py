from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from app.platform.contracts import (
    AssignmentId,
    DisplayName,
    EffectiveInterval,
    FrozenContractModel,
    HistoricalSnapshotContract,
    LocationId,
    PersonId,
    PositiveVersion,
    ResponsibilityGroupId,
    ResponsibilityTypeId,
    SchoolId,
    SemanticCode,
)


class LockedResponsibilityGroupFact(FrozenContractModel):
    """返回已锁定责任组的最小状态、负责人引用和版本。"""

    school_id: SchoolId
    responsibility_group_id: ResponsibilityGroupId
    manager_assignment_id: AssignmentId
    manager_assignment_version: PositiveVersion
    status: Literal["active", "inactive"]
    version: PositiveVersion


class RepairResponsibilityGroupFact(FrozenContractModel):
    """提供报修路由所需的责任组稳定状态，不泄漏负责人或显示信息。"""

    school_id: SchoolId
    responsibility_group_id: ResponsibilityGroupId
    status: Literal["active", "inactive"]
    version: PositiveVersion


class RepairResponsibilityMemberFact(FrozenContractModel):
    """提供当前责任成员与精确任职的最小可锁定事实。"""

    membership_id: UUID
    school_id: SchoolId
    responsibility_group_id: ResponsibilityGroupId
    membership_version: PositiveVersion
    person_id: PersonId
    assignment_id: AssignmentId
    assignment_version: PositiveVersion


class RepairResponsibilityAssignmentFact(FrozenContractModel):
    """证明一条精确任职在同一数据库时点仍然有效。"""

    school_id: SchoolId
    person_id: PersonId
    assignment_id: AssignmentId
    assignment_version: PositiveVersion


class RepairPresentationResponsibilityReader(Protocol):
    """按服务端精确任职读取当前责任关系，不接受字符串角色。"""

    def get_current_repair_assignment(
        self,
        school_id: SchoolId,
        person_id: PersonId,
        assignment_id: AssignmentId,
        assignment_version: PositiveVersion,
        *,
        as_of: datetime,
    ) -> RepairResponsibilityAssignmentFact | None: ...

    def list_current_repair_relationships_for_assignment(
        self,
        school_id: SchoolId,
        person_id: PersonId,
        assignment_id: AssignmentId,
        assignment_version: PositiveVersion,
        *,
        as_of: datetime,
    ) -> tuple[RepairResponsibilityMemberFact, ...]: ...


class RepairRouteResponsibilityReader(Protocol):
    """只读取报修确定性路由需要的责任组和当前成员。"""

    def get_repair_route_groups(
        self,
        school_id: SchoolId,
        group_ids: tuple[ResponsibilityGroupId, ...],
    ) -> tuple[RepairResponsibilityGroupFact, ...]: ...

    def list_current_repair_route_members(
        self,
        school_id: SchoolId,
        group_id: ResponsibilityGroupId,
        *,
        as_of: datetime,
    ) -> tuple[RepairResponsibilityMemberFact, ...]: ...


class RepairRouteResponsibilityLocker(Protocol):
    """按稳定 ID 锁定已由路由器读取的组和成员行。"""

    def lock_repair_route_groups(
        self,
        school_id: SchoolId,
        group_ids: tuple[ResponsibilityGroupId, ...],
    ) -> tuple[RepairResponsibilityGroupFact, ...]: ...

    def lock_repair_route_memberships(
        self,
        school_id: SchoolId,
        membership_ids: tuple[UUID, ...],
        *,
        as_of: datetime,
    ) -> tuple[RepairResponsibilityMemberFact, ...]: ...


class NewConfirmedResponsibilityMembership(FrozenContractModel):
    """限制跨模块 Handler 可写入的责任组成员字段。"""

    membership_id: UUID
    school_id: SchoolId
    responsibility_group_id: ResponsibilityGroupId
    assignment_id: AssignmentId
    interval: EffectiveInterval


class ResponsibilityGroupSnapshot(HistoricalSnapshotContract):
    """保存责任组在指定时点的最小显示快照，不承载业务状态机。"""

    responsibility_group_id: ResponsibilityGroupId
    display_name: DisplayName


class ResponsibilityGroupSnapshotResolver(Protocol):
    """按稳定责任组引用解析历史显示快照。"""

    def resolve_group_snapshot(
        self,
        school_id: SchoolId,
        responsibility_group_id: ResponsibilityGroupId,
        *,
        as_of: datetime,
    ) -> ResponsibilityGroupSnapshot | None:
        """返回指定历史时点的责任组显示快照。"""

        ...


class RepairResponsibilityGrantFact(HistoricalSnapshotContract):
    """返回一条已发布报修责任授权的稳定、窄路由事实。"""

    responsibility_type_id: ResponsibilityTypeId
    responsibility_group_id: ResponsibilityGroupId
    manager_assignment_id: AssignmentId
    manager_assignment_version: PositiveVersion
    location_id: LocationId | None
    repair_subtype: SemanticCode | None
    severity: SemanticCode | None
    route_mode: Literal["direct", "group_claim", "manager_dispatch", "create_and_handle"]


class RepairResponsibilityGrantResolver(Protocol):
    """按确定范围读取责任模块拥有的报修路由授权。"""

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
        """返回最具体的当前授权；没有唯一匹配时不擅自路由。"""

        ...
