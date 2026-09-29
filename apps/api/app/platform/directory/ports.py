from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.platform.contracts import PersonId, SchoolId
from app.platform.directory.contracts import LockedPersonFact, PersonSnapshotResolver
from app.platform.directory.models import AccountPersonBinding, SchoolPerson, SchoolPersonName


class PersonRepository(PersonSnapshotResolver, Protocol):
    """定义自然人、姓名版本和账号绑定所需的最小写入边界。"""

    def add_person(self, person: SchoolPerson) -> None:
        """加入自然人稳定主体并 flush，不提交事务。"""

        ...

    def add_name(self, name: SchoolPersonName) -> None:
        """加入姓名有效期版本并立即触发数据库约束。"""

        ...

    def add_account_binding(self, binding: AccountPersonBinding) -> None:
        """加入账号—自然人绑定并触发同校一对一约束。"""

        ...

    def get_person_for_update(
        self,
        school_id: SchoolId,
        person_id: PersonId,
    ) -> SchoolPerson | None:
        """在学校范围内按自然人 ID 获取行锁。"""

        ...

    def save_person(self, person: SchoolPerson) -> None:
        """flush 自然人状态变更，由外层 UoW 决定提交。"""

        ...


class PersonVersionLockPort(Protocol):
    """只允许跨模块 Handler 锁定并读取人员状态与版本。"""

    def lock_person(
        self,
        school_id: SchoolId,
        person_id: PersonId,
        *,
        as_of: datetime,
    ) -> LockedPersonFact | None:
        """按学校和稳定 ID 锁行，并按数据库时点裁定当前有效性。"""

        ...
