from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.platform.contracts import EffectiveInterval, SchoolId
from app.platform.tenancy.contracts import SchoolSnapshot
from app.platform.tenancy.models import School


class SqlAlchemySchoolRepository:
    """在调用方拥有的 Session 内读写学校稳定主体。"""

    def __init__(self, session: Session) -> None:
        """绑定调用方 Session；Repository 不接管其生命周期。"""

        self._session = session

    def add(self, school: School) -> None:
        """把学校加入现有事务并 flush，不执行 commit。"""

        self._session.add(school)
        self._session.flush()

    def get_for_update(self, school_id: SchoolId) -> School | None:
        """按学校稳定 ID 执行 ``FOR UPDATE`` 锁定读取。"""

        statement = select(School).where(School.id == school_id).with_for_update()
        return self._session.scalar(statement)

    def save(self, school: School) -> None:
        """flush 已修改学校，由调用方 UoW 决定事务结果。"""

        self._session.add(school)
        self._session.flush()

    def get_current_school(
        self,
        school_id: SchoolId,
        *,
        as_of: datetime,
    ) -> SchoolSnapshot | None:
        """按统一时点读取学校内容版本，并以账本终止语义裁定历史。"""

        row = self._session.execute(
            text(
                "SELECT school_id, display_name, timezone_name, source_version, "
                "valid_from, valid_to, captured_at "
                "FROM app.school_fact_school_snapshot_as_of(:school_id, :as_of)"
            ),
            {"school_id": school_id, "as_of": as_of},
        ).one_or_none()
        if row is None:
            return None
        return SchoolSnapshot(
            school_id=SchoolId(row.school_id),
            display_name=str(row.display_name),
            timezone_name=str(row.timezone_name),
            source_version=int(row.source_version),
            captured_at=row.captured_at,
            effective_interval=EffectiveInterval(
                valid_from=row.valid_from,
                valid_to=row.valid_to,
            ),
            snapshot_basis="tenancy.school_as_of",
        )
