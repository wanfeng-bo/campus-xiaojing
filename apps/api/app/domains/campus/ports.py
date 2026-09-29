from __future__ import annotations

from typing import Protocol

from app.domains.campus.contracts import (
    CurrentLocationReader,
    ExactCurrentLocationReader,
    LocationSnapshotResolver,
)
from app.domains.campus.models import CampusLocation, CampusLocationVersion
from app.platform.contracts import LocationId, SchoolId


class CampusRepository(
    CurrentLocationReader,
    ExactCurrentLocationReader,
    LocationSnapshotResolver,
    Protocol,
):
    """定义地点稳定主体、显示版本和窄读取的持久化边界。"""

    def add_location(self, location: CampusLocation) -> None:
        """加入地点稳定主体并 flush。"""

        ...

    def add_location_version(self, version: CampusLocationVersion) -> None:
        """加入地点显示版本并触发有效期约束。"""

        ...

    def get_location_for_update(
        self,
        school_id: SchoolId,
        location_id: LocationId,
    ) -> CampusLocation | None:
        """在学校范围内锁定地点稳定主体。"""

        ...

    def save_location(self, location: CampusLocation) -> None:
        """flush 地点变化，由外层 UoW 决定事务结果。"""

        ...
