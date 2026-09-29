from __future__ import annotations

from typing import Protocol

from app.platform.contracts import ResponsibilityGroupId, SchoolId
from app.platform.responsibility.contracts import (
    LockedResponsibilityGroupFact,
    NewConfirmedResponsibilityMembership,
    RepairPresentationResponsibilityReader,
    RepairResponsibilityGrantResolver,
    RepairRouteResponsibilityLocker,
    RepairRouteResponsibilityReader,
    ResponsibilityGroupSnapshotResolver,
)
from app.platform.responsibility.models import (
    RepairResponsibilityGrant,
    ResponsibilityGroup,
    ResponsibilityGroupMembership,
    ResponsibilityGroupVersion,
    ResponsibilityType,
)


class ConfirmedResponsibilityMembershipReader(Protocol):
    """只按固定 ID 与全部不可变字段核验已确认责任组成员。"""

    def confirmed_membership_matches(
        self,
        membership: NewConfirmedResponsibilityMembership,
    ) -> bool: ...


class ResponsibilityMembershipMutationPort(
    ConfirmedResponsibilityMembershipReader,
    Protocol,
):
    """供单一跨模块 Handler 使用的责任组成员窄写口。"""

    def lock_group(
        self,
        school_id: SchoolId,
        group_id: ResponsibilityGroupId,
    ) -> LockedResponsibilityGroupFact | None: ...

    def add_confirmed_membership(
        self,
        membership: NewConfirmedResponsibilityMembership,
    ) -> None: ...


class ResponsibilityRepository(
    ResponsibilityGroupSnapshotResolver,
    RepairResponsibilityGrantResolver,
    RepairRouteResponsibilityReader,
    RepairRouteResponsibilityLocker,
    RepairPresentationResponsibilityReader,
    ResponsibilityMembershipMutationPort,
    Protocol,
):
    """定义责任事实的最小、调用方事务所有写口。"""

    def add_type(self, responsibility_type: ResponsibilityType) -> None: ...

    def add_group(self, group: ResponsibilityGroup) -> None: ...

    def add_group_version(self, version: ResponsibilityGroupVersion) -> None: ...

    def add_membership(self, membership: ResponsibilityGroupMembership) -> None: ...

    def add_repair_grant(self, grant: RepairResponsibilityGrant) -> None: ...

    def get_group_for_update(
        self,
        school_id: SchoolId,
        group_id: ResponsibilityGroupId,
    ) -> ResponsibilityGroup | None: ...
