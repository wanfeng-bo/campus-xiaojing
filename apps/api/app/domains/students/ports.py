from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.domains.students.contracts import (
    CurrentStudentRelationReader,
    CurrentStudentRosterReader,
    StudentRelationSnapshotResolver,
)
from app.domains.students.models import (
    AdministrativeClass,
    AdministrativeClassVersion,
    GuardianRelationship,
    Student,
    StudentEnrollment,
)
from app.platform.contracts import EnrollmentId, SchoolId


class StudentRepository(
    CurrentStudentRelationReader,
    StudentRelationSnapshotResolver,
    CurrentStudentRosterReader,
    Protocol,
):
    """定义学生、学籍、家校和名单所需的最小持久化边界。"""

    def add_administrative_class(self, administrative_class: AdministrativeClass) -> None:
        """加入行政班稳定主体并 flush。"""

        ...

    def add_administrative_class_version(self, version: AdministrativeClassVersion) -> None:
        """加入行政班显示版本并触发有效期约束。"""

        ...

    def add_student(self, student: Student) -> None:
        """加入学生稳定主体并触发同校自然人约束。"""

        ...

    def add_enrollment(self, enrollment: StudentEnrollment) -> None:
        """加入学籍周期并触发排他区间约束。"""

        ...

    def add_guardian_relationship(self, relationship: GuardianRelationship) -> None:
        """加入经核实的家校关系并触发同校约束。"""

        ...

    def end_enrollment(
        self,
        school_id: SchoolId,
        enrollment_id: EnrollmentId,
        *,
        effective_at: datetime,
    ) -> None:
        """结束指定学籍周期但不删除学生或历史关系。"""

        ...
