from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import Select, Text, cast, func, literal, or_, select, true
from sqlalchemy.engine import Row, RowMapping
from sqlalchemy.orm import Session

from app.domains.students.contracts import (
    CurrentStudentRosterPage,
    StudentRelationSnapshot,
    StudentRosterItem,
)
from app.domains.students.models import (
    AdministrativeClass,
    AdministrativeClassVersion,
    GuardianRelationship,
    Student,
    StudentEnrollment,
)
from app.platform.contracts import (
    AdministrativeClassId,
    EffectiveInterval,
    EnrollmentId,
    PersonId,
    SchoolId,
    StudentId,
)
from app.platform.directory.contracts import PersonSnapshot, PersonSnapshotReader

_ROSTER_PAGE_SIZE = 50


class SqlAlchemyStudentRepository:
    """在调用方事务内维护学生事实并组合窄姓名 Resolver 生成快照。"""

    def __init__(
        self,
        session: Session,
        *,
        person_snapshot_resolver: PersonSnapshotReader | None = None,
    ) -> None:
        """绑定 Session 与可选姓名 Resolver，不创建或结束事务。"""

        self._session = session
        self._person_snapshot_resolver = person_snapshot_resolver

    def add_administrative_class(self, administrative_class: AdministrativeClass) -> None:
        """加入行政班稳定主体并 flush。"""

        self._session.add(administrative_class)
        self._session.flush()

    def add_administrative_class_version(self, version: AdministrativeClassVersion) -> None:
        """加入行政班显示版本并 flush。"""

        self._session.add(version)
        self._session.flush()

    def add_student(self, student: Student) -> None:
        """加入学生稳定主体并 flush。"""

        self._session.add(student)
        self._session.flush()

    def add_enrollment(self, enrollment: StudentEnrollment) -> None:
        """加入学籍周期并 flush，使跨校和重叠错误立即暴露。"""

        self._session.add(enrollment)
        self._session.flush()

    def add_guardian_relationship(self, relationship: GuardianRelationship) -> None:
        """加入家校关系并 flush，使跨校和重叠错误立即暴露。"""

        self._session.add(relationship)
        self._session.flush()

    def end_enrollment(
        self,
        school_id: SchoolId,
        enrollment_id: EnrollmentId,
        *,
        effective_at: datetime,
    ) -> None:
        """锁定并结束学籍周期，不删除学生稳定主体或历史行。"""

        statement = (
            select(StudentEnrollment)
            .where(
                StudentEnrollment.school_id == school_id,
                StudentEnrollment.id == enrollment_id,
            )
            .with_for_update()
        )
        enrollment = self._session.scalar(statement)
        if enrollment is None:
            raise LookupError("指定学籍周期不存在。")
        if effective_at <= enrollment.valid_from:
            raise ValueError("学籍结束时间必须晚于开始时间。")
        if enrollment.valid_to is not None and effective_at > enrollment.valid_to:
            raise ValueError("不能把已结束学籍延长到原结束时间之后。")
        enrollment.valid_to = effective_at
        enrollment.version += 1
        self._session.flush()

    def get_current_student_relation(
        self,
        school_id: SchoolId,
        student_id: StudentId,
        *,
        as_of: datetime,
    ) -> StudentRelationSnapshot | None:
        """解析 active 学生、行政班和同一时点姓名的当前关系。"""

        statement = self._relation_statement(
            school_id=school_id,
            student_id=student_id,
            as_of=as_of,
            require_active=True,
        )
        return self._relation_snapshot_from_row(
            self._session.execute(statement).one_or_none(),
            school_id=school_id,
            as_of=as_of,
        )

    def resolve_student_relation_snapshot(
        self,
        school_id: SchoolId,
        student_id: StudentId,
        *,
        as_of: datetime,
    ) -> StudentRelationSnapshot | None:
        """按历史时点解析关系，不因毕业、改名或班级停用覆盖历史。"""

        statement = self._relation_statement(
            school_id=school_id,
            student_id=student_id,
            as_of=as_of,
            require_active=False,
        )
        return self._relation_snapshot_from_row(
            self._session.execute(statement).one_or_none(),
            school_id=school_id,
            as_of=as_of,
        )

    def get_current_class_roster(
        self,
        school_id: SchoolId,
        administrative_class_id: AdministrativeClassId,
        *,
        as_of: datetime,
        after_student_id: StudentId | None = None,
    ) -> CurrentStudentRosterPage:
        """返回指定行政班在同一时点的总数和稳定五十条名单页。"""

        base_statement = self._roster_base_statement(school_id=school_id, as_of=as_of).where(
            StudentEnrollment.administrative_class_id == administrative_class_id
        )
        return self._execute_roster_page(
            base_statement,
            school_id=school_id,
            as_of=as_of,
            after_student_id=after_student_id,
        )

    def get_current_grade_roster(
        self,
        school_id: SchoolId,
        grade_code: str,
        *,
        as_of: datetime,
        after_student_id: StudentId | None = None,
    ) -> CurrentStudentRosterPage:
        """返回指定年级在同一时点的总数和稳定五十条名单页。"""

        base_statement = self._roster_base_statement(school_id=school_id, as_of=as_of).where(
            AdministrativeClass.grade_code == grade_code
        )
        return self._execute_roster_page(
            base_statement,
            school_id=school_id,
            as_of=as_of,
            after_student_id=after_student_id,
        )

    @staticmethod
    def _relation_statement(
        *,
        school_id: SchoolId,
        student_id: StudentId,
        as_of: datetime,
        require_active: bool,
    ):
        """构造只读取 student owner 私有表的关系查询。"""

        statement = (
            select(
                Student,
                StudentEnrollment,
                AdministrativeClassVersion,
                func.clock_timestamp().label("captured_at"),
            )
            .join(
                StudentEnrollment,
                (StudentEnrollment.school_id == Student.school_id)
                & (StudentEnrollment.student_id == Student.id),
            )
            .join(
                AdministrativeClassVersion,
                (AdministrativeClassVersion.school_id == StudentEnrollment.school_id)
                & (
                    AdministrativeClassVersion.administrative_class_id
                    == StudentEnrollment.administrative_class_id
                ),
            )
            .where(
                Student.school_id == school_id,
                Student.id == student_id,
                func.app.school_fact_root_is_effective_at(
                    Student.school_id,
                    cast(literal("student"), Text),
                    Student.id,
                    as_of,
                ),
                StudentEnrollment.status == "confirmed",
                StudentEnrollment.valid_from <= as_of,
                or_(StudentEnrollment.valid_to.is_(None), StudentEnrollment.valid_to > as_of),
                AdministrativeClassVersion.status == "confirmed",
                AdministrativeClassVersion.valid_from <= as_of,
                or_(
                    AdministrativeClassVersion.valid_to.is_(None),
                    AdministrativeClassVersion.valid_to > as_of,
                ),
            )
        )
        if require_active:
            statement = statement.join(
                AdministrativeClass,
                (AdministrativeClass.school_id == StudentEnrollment.school_id)
                & (AdministrativeClass.id == StudentEnrollment.administrative_class_id),
            ).where(
                AdministrativeClass.status == "active",
            )
        return statement

    def _relation_snapshot_from_row(
        self,
        row: Row[tuple[Student, StudentEnrollment, AdministrativeClassVersion, datetime]] | None,
        *,
        school_id: SchoolId,
        as_of: datetime,
    ) -> StudentRelationSnapshot | None:
        """通过目录公开 Resolver 压平学生、学籍、班级和姓名版本。"""

        if row is None:
            return None
        student, enrollment, class_version, captured_at = row
        resolver = self._require_person_snapshot_resolver()
        person_snapshot = resolver.resolve_person_snapshot(
            school_id,
            PersonId(student.person_id),
            as_of=as_of,
        )
        if person_snapshot is None:
            return None
        return StudentRelationSnapshot(
            school_id=school_id,
            student_id=StudentId(student.id),
            enrollment_id=EnrollmentId(enrollment.id),
            administrative_class_id=AdministrativeClassId(enrollment.administrative_class_id),
            display_name=person_snapshot.display_name,
            administrative_class_display_name=class_version.display_name,
            used_recorded_at_fallback=False,
            source_version=enrollment.version,
            captured_at=captured_at,
            effective_interval=EffectiveInterval(
                valid_from=enrollment.valid_from,
                valid_to=enrollment.valid_to,
            ),
            snapshot_basis="student.enrollment_as_of",
        )

    @staticmethod
    def _roster_base_statement(*, school_id: SchoolId, as_of: datetime) -> Select[Any]:
        """构造当前名单的单一过滤定义，供班级和年级入口复用。"""

        return (
            select(
                Student.id.label("student_id"),
                Student.person_id.label("person_id"),
                StudentEnrollment.id.label("enrollment_id"),
                StudentEnrollment.administrative_class_id.label("administrative_class_id"),
                StudentEnrollment.student_number.label("student_number"),
                StudentEnrollment.seat_number.label("seat_number"),
                AdministrativeClassVersion.display_name.label("class_display_name"),
            )
            .join(
                StudentEnrollment,
                (StudentEnrollment.school_id == Student.school_id)
                & (StudentEnrollment.student_id == Student.id),
            )
            .join(
                AdministrativeClass,
                (AdministrativeClass.school_id == StudentEnrollment.school_id)
                & (AdministrativeClass.id == StudentEnrollment.administrative_class_id),
            )
            .join(
                AdministrativeClassVersion,
                (AdministrativeClassVersion.school_id == AdministrativeClass.school_id)
                & (AdministrativeClassVersion.administrative_class_id == AdministrativeClass.id),
            )
            .where(
                Student.school_id == school_id,
                func.app.school_fact_root_is_effective_at(
                    Student.school_id,
                    cast(literal("student"), Text),
                    Student.id,
                    as_of,
                ),
                StudentEnrollment.status == "confirmed",
                StudentEnrollment.valid_from <= as_of,
                or_(StudentEnrollment.valid_to.is_(None), StudentEnrollment.valid_to > as_of),
                AdministrativeClass.status == "active",
                AdministrativeClassVersion.status == "confirmed",
                AdministrativeClassVersion.valid_from <= as_of,
                or_(
                    AdministrativeClassVersion.valid_to.is_(None),
                    AdministrativeClassVersion.valid_to > as_of,
                ),
            )
        )

    def _execute_roster_page(
        self,
        base_statement: Select[Any],
        *,
        school_id: SchoolId,
        as_of: datetime,
        after_student_id: StudentId | None,
    ) -> CurrentStudentRosterPage:
        """在窗口总数之后应用游标，避免第二页总数随游标缩小。"""

        roster = base_statement.cte("current_student_roster")
        page_statement = select(roster)
        if after_student_id is not None:
            page_statement = page_statement.where(roster.c.student_id > after_student_id)
        page = (
            page_statement.order_by(roster.c.student_id)
            .limit(_ROSTER_PAGE_SIZE + 1)
            .cte("current_student_roster_page")
        )
        total = (
            select(func.count().label("total_count"))
            .select_from(roster)
            .cte("current_student_roster_total")
        )
        statement = (
            select(page, total.c.total_count)
            .select_from(total)
            .outerjoin(page, true())
            .order_by(page.c.student_id)
        )
        rows = self._session.execute(statement).mappings().all()
        page_rows = [row for row in rows if row["student_id"] is not None]
        visible_rows = page_rows[:_ROSTER_PAGE_SIZE]
        person_ids = tuple(PersonId(row["person_id"]) for row in visible_rows)
        person_snapshots = self._require_person_snapshot_resolver().resolve_person_snapshots(
            school_id,
            person_ids,
            as_of=as_of,
        )
        items = tuple(
            self._roster_item_from_mapping(row, person_snapshots)
            for row in visible_rows
            if PersonId(row["person_id"]) in person_snapshots
        )
        if len(items) != len(visible_rows):
            raise RuntimeError("当前名单存在无法解析的确认姓名版本。")
        total_count = int(rows[0]["total_count"])
        next_cursor = items[-1].student_id if len(page_rows) > _ROSTER_PAGE_SIZE else None
        return CurrentStudentRosterPage(
            school_id=school_id,
            as_of=as_of,
            total_count=total_count,
            items=items,
            next_after_student_id=next_cursor,
        )

    @staticmethod
    def _roster_item_from_mapping(
        row: RowMapping,
        person_snapshots: Mapping[PersonId, PersonSnapshot],
    ) -> StudentRosterItem:
        """生成不含完整学号和敏感资料的名单项。"""

        person_snapshot = person_snapshots[PersonId(row["person_id"])]
        return StudentRosterItem(
            student_id=StudentId(row["student_id"]),
            enrollment_id=EnrollmentId(row["enrollment_id"]),
            administrative_class_id=AdministrativeClassId(row["administrative_class_id"]),
            display_name=person_snapshot.display_name,
            administrative_class_display_name=row["class_display_name"],
            disambiguation_suffix=SqlAlchemyStudentRepository._disambiguation_suffix(row),
        )

    @staticmethod
    def _disambiguation_suffix(row: RowMapping) -> str:
        """优先返回座号，其次返回学号尾号，最后使用稳定 ID 尾号。"""

        seat_number = row["seat_number"]
        if isinstance(seat_number, str) and seat_number:
            return f"座号{seat_number}"
        student_number = row["student_number"]
        if isinstance(student_number, str) and student_number:
            return f"学号尾号{student_number[-4:]}"
        return f"编号尾号{str(row['student_id'])[-6:]}"

    def _require_person_snapshot_resolver(self) -> PersonSnapshotReader:
        """读取快照时要求显式注入目录公开 Resolver。"""

        if self._person_snapshot_resolver is None:
            raise RuntimeError("学生快照读取需要目录模块的公开姓名 Resolver。")
        return self._person_snapshot_resolver
