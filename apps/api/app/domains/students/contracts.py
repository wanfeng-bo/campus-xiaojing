from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol

from pydantic import AwareDatetime, Field, StrictBool

from app.platform.contracts import (
    AdministrativeClassId,
    DisplayName,
    EnrollmentId,
    FrozenContractModel,
    HistoricalSnapshotContract,
    SchoolId,
    StudentId,
)

DisambiguationSuffix = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=40),
]


class StudentRelationInput(FrozenContractModel):
    """表达业务表单选择的中性学生稳定引用，不接受客户端快照。"""

    student_id: StudentId


class StudentRelationSnapshot(HistoricalSnapshotContract):
    """保存学生与当时行政班关系的最小历史证据。"""

    student_id: StudentId
    enrollment_id: EnrollmentId
    administrative_class_id: AdministrativeClassId
    display_name: DisplayName
    administrative_class_display_name: DisplayName
    used_recorded_at_fallback: StrictBool


class StudentRosterItem(FrozenContractModel):
    """表示当前名单页中用于人工消歧的最小学生投影。"""

    student_id: StudentId
    enrollment_id: EnrollmentId
    administrative_class_id: AdministrativeClassId
    display_name: DisplayName
    administrative_class_display_name: DisplayName
    disambiguation_suffix: DisambiguationSuffix


class CurrentStudentRosterPage(FrozenContractModel):
    """绑定同一查询时点、总数和固定上限名单页。"""

    school_id: SchoolId
    as_of: AwareDatetime
    total_count: Annotated[int, Field(strict=True, ge=0)]
    items: tuple[StudentRosterItem, ...]
    next_after_student_id: StudentId | None


class CurrentStudentRelationReader(Protocol):
    """只读取当前业务选择器所需的学生最小关系。"""

    def get_current_student_relation(
        self,
        school_id: SchoolId,
        student_id: StudentId,
        *,
        as_of: datetime,
    ) -> StudentRelationSnapshot | None:
        """返回当前学生关系；毕业或关系已结束时返回 ``None``。"""

        ...


class StudentRelationSnapshotResolver(Protocol):
    """按稳定学生引用解析历史关系，不提供当前姓名查询捷径。"""

    def resolve_student_relation_snapshot(
        self,
        school_id: SchoolId,
        student_id: StudentId,
        *,
        as_of: datetime,
    ) -> StudentRelationSnapshot | None:
        """返回指定历史时点的学生关系快照。"""

        ...


class CurrentStudentRosterReader(Protocol):
    """按同一查询时点读取行政班或年级当前名单。"""

    def get_current_class_roster(
        self,
        school_id: SchoolId,
        administrative_class_id: AdministrativeClassId,
        *,
        as_of: datetime,
        after_student_id: StudentId | None = None,
    ) -> CurrentStudentRosterPage:
        """返回行政班总数和最多五十条稳定游标名单。"""

        ...

    def get_current_grade_roster(
        self,
        school_id: SchoolId,
        grade_code: str,
        *,
        as_of: datetime,
        after_student_id: StudentId | None = None,
    ) -> CurrentStudentRosterPage:
        """返回年级总数和最多五十条稳定游标名单。"""

        ...
