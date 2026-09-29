from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from app.platform.contracts import (
    DisplayName,
    FrozenContractModel,
    HistoricalSnapshotContract,
    PersonId,
    PositiveVersion,
    SchoolId,
)


class LockedPersonFact(FrozenContractModel):
    """返回跨模块确认已经锁定的最小人员稳定事实。"""

    school_id: SchoolId
    person_id: PersonId
    status: Literal["active", "inactive", "merged"]
    version: PositiveVersion


class PersonSnapshot(HistoricalSnapshotContract):
    """保存自然人在指定历史时点的最小姓名快照。"""

    person_id: PersonId
    display_name: DisplayName


class LoginMappingSnapshot(HistoricalSnapshotContract):
    """保存账号—自然人绑定在指定时点的完整正式内容。"""

    binding_id: UUID
    account_user_id: int
    person_id: PersonId
    provider_code: str
    provider_tenant_key: str
    external_subject: str
    status: Literal["draft", "confirmed", "rejected"]


class LoginMappingSnapshotResolver(Protocol):
    """按稳定登录映射引用解析指定时点的完整内容切片。"""

    def resolve_login_mapping_snapshot(
        self,
        school_id: SchoolId,
        binding_id: UUID,
        *,
        as_of: datetime,
    ) -> LoginMappingSnapshot | None:
        """返回指定时点成立的登录映射；终止与撤销由账本语义裁定。"""

        ...


class PersonSnapshotResolver(Protocol):
    """按稳定自然人引用和历史时点解析不可变姓名快照。"""

    def resolve_person_snapshot(
        self,
        school_id: SchoolId,
        person_id: PersonId,
        *,
        as_of: datetime,
    ) -> PersonSnapshot | None:
        """返回历史时点快照，不使用当前姓名覆盖历史。"""

        ...


class PersonSnapshotBatchResolver(Protocol):
    """按同一学校和历史时点批量解析姓名，避免名单逐人查询。"""

    def resolve_person_snapshots(
        self,
        school_id: SchoolId,
        person_ids: tuple[PersonId, ...],
        *,
        as_of: datetime,
    ) -> Mapping[PersonId, PersonSnapshot]:
        """返回存在确认姓名版本的人员映射，不补造缺失姓名。"""

        ...


class PersonSnapshotReader(PersonSnapshotResolver, PersonSnapshotBatchResolver, Protocol):
    """组合单项和批量历史姓名解析能力，不提供当前姓名捷径。"""

    pass
