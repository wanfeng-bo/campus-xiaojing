from __future__ import annotations

from typing import Protocol

from app.platform.contracts import SchoolId
from app.platform.tenancy.contracts import CurrentSchoolReader
from app.platform.tenancy.models import School


class SchoolRepository(CurrentSchoolReader, Protocol):
    """定义学校稳定主体所需的最小写入与锁定边界。"""

    def add(self, school: School) -> None:
        """把学校加入调用方事务并 flush，但不提交事务。"""

        ...

    def get_for_update(self, school_id: SchoolId) -> School | None:
        """按稳定学校 ID 获取行锁；不存在时返回 ``None``。"""

        ...

    def save(self, school: School) -> None:
        """flush 已变更学校，由外层 UoW 决定提交或回滚。"""

        ...
