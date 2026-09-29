from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import StringConstraints

from app.platform.contracts import (
    AssignmentId,
    DisplayName,
    EffectiveInterval,
    FrozenContractModel,
    HistoricalSnapshotContract,
    OrganizationUnitId,
    PersonId,
    PositiveVersion,
    ResponsibilityGroupId,
    SchoolId,
    SemanticCode,
)

ConfirmationReference = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]


class ConfirmStaffDutyBindingCommand(FrozenContractModel):
    """表达人工确认卡明确列出的任职与责任组成员连带变化。"""

    person_id: PersonId
    organization_unit_id: OrganizationUnitId
    position_title: DisplayName
    semantic_role_code: SemanticCode
    responsibility_group_id: ResponsibilityGroupId
    interval: EffectiveInterval
    expected_person_version: PositiveVersion
    expected_organization_unit_version: PositiveVersion
    expected_group_version: PositiveVersion
    confirmation_reason: ConfirmationReference


class ConfirmStaffDutyBindingOutcome(FrozenContractModel):
    """返回首次提交或安全重放得到的三个稳定事实引用。"""

    kind: Literal["created", "replayed"]
    assignment_id: AssignmentId
    membership_id: UUID
    outbox_event_id: UUID
    assignment_version: PositiveVersion
    membership_version: PositiveVersion


class LockedStaffAssignmentFact(FrozenContractModel):
    """返回已锁定任职用于当前操作者重验的最小事实。"""

    school_id: SchoolId
    assignment_id: AssignmentId
    person_id: PersonId
    organization_unit_id: OrganizationUnitId
    semantic_role_code: SemanticCode
    status: Literal["draft", "confirmed", "rejected"]
    version: PositiveVersion
    interval: EffectiveInterval


class LockedOrganizationUnitFact(FrozenContractModel):
    """返回确认卡目标组织的稳定状态与版本。"""

    school_id: SchoolId
    organization_unit_id: OrganizationUnitId
    status: Literal["active", "inactive"]
    version: PositiveVersion


class NewConfirmedStaffAssignment(FrozenContractModel):
    """限制组织 Repository 只能写入本 Handler 已验证的正式任职字段。"""

    assignment_id: AssignmentId
    school_id: SchoolId
    person_id: PersonId
    organization_unit_id: OrganizationUnitId
    position_title: DisplayName
    semantic_role_code: SemanticCode
    interval: EffectiveInterval
    approval_reference: ConfirmationReference


class StaffDutyBindingAuthorizationFacts(FrozenContractModel):
    """向服务端授权策略提供不含姓名和客户端权限标志的结构化事实。"""

    school_id: SchoolId
    actor_assignment_id: AssignmentId
    actor_semantic_role_code: SemanticCode
    target_person_id: PersonId
    target_organization_unit_id: OrganizationUnitId
    responsibility_group_id: ResponsibilityGroupId


class StaffDutyBindingAuthorizer(Protocol):
    """由服务端配置决定一项当前任职是否能确认该连带变化。"""

    def is_allowed(self, facts: StaffDutyBindingAuthorizationFacts) -> bool: ...


class AssignmentSnapshot(HistoricalSnapshotContract):
    """保存正式任职及其当时组织、岗位显示信息。"""

    assignment_id: AssignmentId
    person_id: PersonId
    organization_unit_id: OrganizationUnitId
    organization_display_name: DisplayName
    position_title: DisplayName
    semantic_role_code: SemanticCode
    assignment_kind: str
    appointment_source: str
    approval_reference: str
    planned_valid_to: datetime | None
    status: Literal["draft", "confirmed", "rejected"]


class OrganizationUnitSnapshot(HistoricalSnapshotContract):
    """保存组织单位在指定时点的父级与显示内容。"""

    organization_unit_id: OrganizationUnitId
    parent_organization_unit_id: OrganizationUnitId | None
    display_name: DisplayName


class OrganizationUnitSnapshotResolver(Protocol):
    """按稳定组织引用解析历史结构和显示内容。"""

    def resolve_organization_unit_snapshot(
        self,
        school_id: SchoolId,
        organization_unit_id: OrganizationUnitId,
        *,
        as_of: datetime,
    ) -> OrganizationUnitSnapshot | None:
        """返回指定历史时点的组织快照。"""

        ...


class CurrentAssignmentReader(Protocol):
    """只读取 organization 所拥有的当前正式任职与组织状态。"""

    def get_current_assignment(
        self,
        school_id: SchoolId,
        assignment_id: AssignmentId,
        *,
        as_of: datetime,
    ) -> AssignmentSnapshot | None:
        """返回任职快照；完整授权还须由上下文解析器核验账号、学校和自然人。"""

        ...


class AssignmentSnapshotResolver(Protocol):
    """按历史时点解析任职及当时组织显示信息。"""

    def resolve_assignment_snapshot(
        self,
        school_id: SchoolId,
        assignment_id: AssignmentId,
        *,
        as_of: datetime,
    ) -> AssignmentSnapshot | None:
        """返回历史任职快照，不借旧任职重新授予当前权限。"""

        ...
