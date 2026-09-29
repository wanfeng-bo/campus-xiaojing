from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.platform.contracts import (
    DisplayName,
    DisplayPath,
    FrozenContractModel,
    HistoricalSnapshotContract,
    LocationId,
    PositiveVersion,
    SchoolId,
)


class LocationSnapshot(HistoricalSnapshotContract):
    """保存地点当时的结构、名称和路径，不混入人员或责任事实。"""

    location_id: LocationId
    parent_location_id: LocationId | None
    kind: str
    display_name: DisplayName
    display_path: DisplayPath


class ExactLocationMatch(FrozenContractModel):
    """返回精确名称或别名命中的当前地点稳定引用，不携带责任结论。"""

    school_id: SchoolId
    location_id: LocationId
    source_version: PositiveVersion


class ExactCurrentLocationReader(Protocol):
    """只做当前地点的精确匹配与稳定引用核验。"""

    def find_exact_locations(
        self,
        school_id: SchoolId,
        *,
        location_text: str,
        as_of: datetime,
    ) -> tuple[ExactLocationMatch, ...]:
        """最多返回两个精确命中，使调用方能够稳定识别歧义。"""

        ...

    def location_is_current(
        self,
        school_id: SchoolId,
        location_id: LocationId,
        *,
        as_of: datetime,
    ) -> bool:
        """核验服务端取得的地点引用在当前时点仍有效。"""

        ...


class CurrentLocationReader(Protocol):
    """只读取指定学校和时点的当前有效地点。"""

    def get_current_location(
        self,
        school_id: SchoolId,
        location_id: LocationId,
        *,
        as_of: datetime,
    ) -> LocationSnapshot | None:
        """返回当前地点最小快照，不解析任何责任人。"""

        ...


class LocationSnapshotResolver(Protocol):
    """按稳定地点引用解析历史名称和路径。"""

    def resolve_location_snapshot(
        self,
        school_id: SchoolId,
        location_id: LocationId,
        *,
        as_of: datetime,
    ) -> LocationSnapshot | None:
        """返回指定历史时点的地点快照。"""

        ...
