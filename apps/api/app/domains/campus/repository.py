from __future__ import annotations

from datetime import datetime

from sqlalchemy import any_, func, or_, select, text
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from app.domains.campus.contracts import ExactLocationMatch, LocationSnapshot
from app.domains.campus.models import CampusLocation, CampusLocationVersion
from app.platform.contracts import EffectiveInterval, LocationId, SchoolId


class SqlAlchemyCampusRepository:
    """在调用方事务内维护地点事实并解析当前或历史快照。"""

    def __init__(self, session: Session) -> None:
        """绑定调用方 Session，不接管事务或会话生命周期。"""

        self._session = session

    def add_location(self, location: CampusLocation) -> None:
        """把地点稳定主体加入现有事务并 flush。"""

        self._session.add(location)
        self._session.flush()

    def add_location_version(self, version: CampusLocationVersion) -> None:
        """加入地点显示版本并 flush，使区间冲突立即暴露。"""

        self._session.add(version)
        self._session.flush()

    def get_location_for_update(
        self,
        school_id: SchoolId,
        location_id: LocationId,
    ) -> CampusLocation | None:
        """按学校和地点稳定 ID 执行行锁读取。"""

        statement = (
            select(CampusLocation)
            .where(
                CampusLocation.school_id == school_id,
                CampusLocation.id == location_id,
            )
            .with_for_update()
        )
        return self._session.scalar(statement)

    def save_location(self, location: CampusLocation) -> None:
        """flush 地点变化，不执行 commit 或 rollback。"""

        self._session.add(location)
        self._session.flush()

    def get_current_location(
        self,
        school_id: SchoolId,
        location_id: LocationId,
        *,
        as_of: datetime,
    ) -> LocationSnapshot | None:
        """读取当前 active 地点和同一时点的 confirmed 显示版本。"""

        statement = self._snapshot_statement(
            school_id=school_id,
            location_id=location_id,
            as_of=as_of,
        )
        return self._snapshot_from_row(self._session.execute(statement).one_or_none())

    def find_exact_locations(
        self,
        school_id: SchoolId,
        *,
        location_text: str,
        as_of: datetime,
    ) -> tuple[ExactLocationMatch, ...]:
        """按当前名称或别名精确匹配，最多返回两个以显式暴露歧义。"""

        statement = (
            select(CampusLocationVersion.location_id, CampusLocationVersion.version)
            .where(
                CampusLocationVersion.school_id == school_id,
                CampusLocationVersion.status == "confirmed",
                CampusLocationVersion.valid_from <= as_of,
                or_(
                    CampusLocationVersion.valid_to.is_(None),
                    CampusLocationVersion.valid_to > as_of,
                ),
                or_(
                    CampusLocationVersion.display_name == location_text,
                    any_(CampusLocationVersion.aliases) == location_text,
                ),
                text(
                    "app.school_fact_location_history_is_resolvable("
                    "campus_location_versions.school_id, "
                    "campus_location_versions.location_id, :exact_location_as_of)"
                ).bindparams(exact_location_as_of=as_of),
            )
            .order_by(CampusLocationVersion.location_id)
            .limit(2)
        )
        return tuple(
            ExactLocationMatch(
                school_id=school_id,
                location_id=LocationId(location_id),
                source_version=version,
            )
            for location_id, version in self._session.execute(statement)
        )

    def location_is_current(
        self,
        school_id: SchoolId,
        location_id: LocationId,
        *,
        as_of: datetime,
    ) -> bool:
        """只核验当前地点引用，不把地点文字或相似度当作授权。"""

        return self.get_current_location(school_id, location_id, as_of=as_of) is not None

    def resolve_location_snapshot(
        self,
        school_id: SchoolId,
        location_id: LocationId,
        *,
        as_of: datetime,
    ) -> LocationSnapshot | None:
        """按历史时点解析地点，不因当前停用或改名覆盖历史。"""

        statement = select(
            CampusLocationVersion,
            func.clock_timestamp().label("captured_at"),
        ).where(
            CampusLocationVersion.school_id == school_id,
            CampusLocationVersion.location_id == location_id,
            CampusLocationVersion.status == "confirmed",
            CampusLocationVersion.valid_from <= as_of,
            or_(
                CampusLocationVersion.valid_to.is_(None),
                CampusLocationVersion.valid_to > as_of,
            ),
            text(
                "NOT EXISTS (SELECT 1 FROM "
                "app.school_fact_content_version_terminal("
                "campus_location_versions.school_id, 'location', "
                "campus_location_versions.location_id, campus_location_versions.version) "
                "AS terminal WHERE terminal.operation = 'revoke' "
                "OR terminal.effective_at IS NULL "
                "OR terminal.effective_at <= :historical_terminal_as_of)"
            ).bindparams(historical_terminal_as_of=as_of),
            text(
                "app.school_fact_location_history_is_resolvable("
                "campus_location_versions.school_id, "
                "campus_location_versions.location_id, "
                ":historical_ancestry_as_of)"
            ).bindparams(historical_ancestry_as_of=as_of),
        )
        row = self._session.execute(statement).one_or_none()
        if row is None:
            return None
        version, captured_at = row
        return self._snapshot_from_version(version, captured_at=captured_at)

    @staticmethod
    def _snapshot_statement(
        *,
        school_id: SchoolId,
        location_id: LocationId,
        as_of: datetime,
    ):
        """构造以结构版本与账本终止语义为权威的时点快照查询。"""

        statement = select(
            CampusLocationVersion,
            func.clock_timestamp().label("captured_at"),
        ).where(
            CampusLocationVersion.school_id == school_id,
            CampusLocationVersion.location_id == location_id,
            CampusLocationVersion.status == "confirmed",
            CampusLocationVersion.valid_from <= as_of,
            or_(
                CampusLocationVersion.valid_to.is_(None),
                CampusLocationVersion.valid_to > as_of,
            ),
            text(
                "app.school_fact_location_history_is_resolvable("
                "campus_location_versions.school_id, "
                "campus_location_versions.location_id, :current_location_as_of)"
            ).bindparams(current_location_as_of=as_of),
        )
        return statement

    @staticmethod
    def _snapshot_from_row(
        row: Row[tuple[CampusLocationVersion, datetime]] | None,
    ) -> LocationSnapshot | None:
        """把地点稳定主体和显示版本压平为不可变快照。"""

        if row is None:
            return None
        version, captured_at = row
        return SqlAlchemyCampusRepository._snapshot_from_version(
            version,
            captured_at=captured_at,
        )

    @staticmethod
    def _snapshot_from_version(
        version: CampusLocationVersion,
        *,
        captured_at: datetime,
    ) -> LocationSnapshot:
        """把地点显示版本压平为不依赖当前地点状态的历史快照。"""

        return LocationSnapshot(
            school_id=SchoolId(version.school_id),
            location_id=LocationId(version.location_id),
            parent_location_id=(
                LocationId(version.parent_location_id)
                if version.parent_location_id is not None
                else None
            ),
            kind=version.kind,
            display_name=version.display_name,
            display_path=version.display_path,
            source_version=version.version,
            captured_at=captured_at,
            effective_interval=EffectiveInterval(
                valid_from=version.valid_from,
                valid_to=version.valid_to,
            ),
            snapshot_basis="campus.location_version_as_of",
        )
