from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.domains.campus.contracts import LocationSnapshot
from app.domains.students.contracts import StudentRelationSnapshot
from app.platform.calendar.contracts import CalendarNotPublished, WorkCalendarSnapshot
from app.platform.contracts import (
    AdministrativeClassId,
    EnrollmentId,
    LocationId,
    SchoolId,
    StudentId,
)
from app.platform.directory.models import SchoolPerson, SchoolPersonName
from app.platform.directory.repository import SqlAlchemyPersonRepository
from app.platform.tenancy.models import School
from tests.master_data.test_execution_context import (
    _seed_formal_structure_lineage,  # pyright: ignore[reportPrivateUsage]
)
from tests.master_data.test_student_campus_calendar_schema import (
    _insert_location_version,  # pyright: ignore[reportPrivateUsage]
    _insert_structure_actor,  # pyright: ignore[reportPrivateUsage]
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = T0 + timedelta(days=30)
T2 = T1 + timedelta(days=30)
T3 = T2 + timedelta(days=30)


def _school(school_id: UUID, *, code: str) -> School:
    return School(
        id=school_id,
        school_code=code,
        display_name=f"学校-{code}",
        timezone_name="Asia/Shanghai",
        status="active",
        version=1,
        created_at=T0,
        updated_at=T0,
    )


@pytest.mark.integration
def test_future_calendar_becomes_current_at_boundary_without_activation_worker(
    current_owner_session: Session,
) -> None:
    calendar_models = importlib.import_module("app.platform.calendar.models")
    calendar_repository = importlib.import_module("app.platform.calendar.repository")
    school_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    draft_id = uuid4()
    current_owner_session.add(_school(school_id, code="future-calendar"))
    current_owner_session.flush()
    repository = calendar_repository.SqlAlchemyWorkCalendarRepository(current_owner_session)
    repository.add_version(
        calendar_models.SchoolCalendarVersion(
            id=first_id,
            school_id=school_id,
            display_name="第一版校历",
            timezone_name="Asia/Shanghai",
            valid_from=T0,
            valid_to=T1,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="published",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    repository.add_version(
        calendar_models.SchoolCalendarVersion(
            id=second_id,
            school_id=school_id,
            display_name="第二版校历",
            timezone_name="Asia/Shanghai",
            valid_from=T1,
            valid_to=None,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="published",
            version=2,
            created_at=T0,
            updated_at=T0,
        )
    )
    repository.add_version(
        calendar_models.SchoolCalendarVersion(
            id=draft_id,
            school_id=school_id,
            display_name="未发布草稿",
            timezone_name="Asia/Shanghai",
            valid_from=T0,
            valid_to=None,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="draft",
            version=3,
            created_at=T0,
            updated_at=T0,
        )
    )

    before = repository.get_published_snapshot(
        SchoolId(school_id), as_of=T1 - timedelta(microseconds=1)
    )
    at_boundary = repository.get_published_snapshot(SchoolId(school_id), as_of=T1)
    assert isinstance(before, WorkCalendarSnapshot)
    assert before.calendar_version_id == first_id
    assert isinstance(at_boundary, WorkCalendarSnapshot)
    assert at_boundary.calendar_version_id == second_id


@pytest.mark.integration
def test_calendar_reader_uses_database_clock_once_when_as_of_is_omitted(
    current_owner_session: Session,
) -> None:
    calendar_models = importlib.import_module("app.platform.calendar.models")
    calendar_repository = importlib.import_module("app.platform.calendar.repository")
    school_id = uuid4()
    calendar_id = uuid4()
    current_owner_session.add(_school(school_id, code="database-clock"))
    current_owner_session.flush()
    repository = calendar_repository.SqlAlchemyWorkCalendarRepository(current_owner_session)
    repository.add_version(
        calendar_models.SchoolCalendarVersion(
            id=calendar_id,
            school_id=school_id,
            display_name="当前校历",
            timezone_name="Asia/Shanghai",
            valid_from=datetime(2020, 1, 1, tzinfo=UTC),
            valid_to=None,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="published",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    statements: list[str] = []
    connection = current_owner_session.connection()

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", record_statement)
    try:
        result = repository.get_published_snapshot(SchoolId(school_id))
    finally:
        event.remove(connection, "before_cursor_execute", record_statement)

    assert isinstance(result, WorkCalendarSnapshot)
    assert result.calendar_version_id == calendar_id
    assert sum("clock_timestamp" in statement.lower() for statement in statements) == 1


@pytest.mark.integration
def test_draft_only_calendar_returns_typed_not_published(
    current_owner_session: Session,
) -> None:
    calendar_models = importlib.import_module("app.platform.calendar.models")
    calendar_repository = importlib.import_module("app.platform.calendar.repository")
    school_id = uuid4()
    current_owner_session.add(_school(school_id, code="draft-calendar"))
    current_owner_session.flush()
    repository = calendar_repository.SqlAlchemyWorkCalendarRepository(current_owner_session)
    repository.add_version(
        calendar_models.SchoolCalendarVersion(
            id=uuid4(),
            school_id=school_id,
            display_name="草稿校历",
            timezone_name="Asia/Shanghai",
            valid_from=T0,
            valid_to=None,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="draft",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    result = repository.get_published_snapshot(SchoolId(school_id), as_of=T1)
    assert isinstance(result, CalendarNotPublished)
    assert result.school_id == school_id
    assert result.as_of == T1


@pytest.mark.integration
def test_student_snapshot_uses_historical_person_and_class_versions(
    current_owner_session: Session,
) -> None:
    student_models = importlib.import_module("app.domains.students.models")
    student_repository = importlib.import_module("app.domains.students.repository")
    school_id = uuid4()
    person_id = uuid4()
    student_id = uuid4()
    class_id = uuid4()
    enrollment_id = uuid4()
    current_owner_session.add(_school(school_id, code="student-history"))
    current_owner_session.flush()
    person_repository = SqlAlchemyPersonRepository(current_owner_session)
    person_repository.add_person(
        SchoolPerson(
            id=person_id,
            school_id=school_id,
            status="active",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    person_repository.add_name(
        SchoolPersonName(
            id=uuid4(),
            school_id=school_id,
            person_id=person_id,
            display_name="李明",
            valid_from=T0,
            valid_to=T1,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="confirmed",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    person_repository.add_name(
        SchoolPersonName(
            id=uuid4(),
            school_id=school_id,
            person_id=person_id,
            display_name="李小明",
            valid_from=T1,
            valid_to=None,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="confirmed",
            version=2,
            created_at=T1,
            updated_at=T1,
        )
    )
    repository = student_repository.SqlAlchemyStudentRepository(
        current_owner_session,
        person_snapshot_resolver=person_repository,
    )
    repository.add_administrative_class(
        student_models.AdministrativeClass(
            id=class_id,
            school_id=school_id,
            school_year_code="2025-2026",
            grade_code="grade-7",
            status="active",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    repository.add_administrative_class_version(
        student_models.AdministrativeClassVersion(
            id=uuid4(),
            school_id=school_id,
            administrative_class_id=class_id,
            display_name="初一（3）班",
            short_name="初一3班",
            valid_from=T0,
            valid_to=T1,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="confirmed",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    repository.add_administrative_class_version(
        student_models.AdministrativeClassVersion(
            id=uuid4(),
            school_id=school_id,
            administrative_class_id=class_id,
            display_name="七年级（3）班",
            short_name="七3",
            valid_from=T1,
            valid_to=None,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="confirmed",
            version=2,
            created_at=T1,
            updated_at=T1,
        )
    )
    repository.add_student(
        student_models.Student(
            id=student_id,
            school_id=school_id,
            person_id=person_id,
            status="active",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    repository.add_enrollment(
        student_models.StudentEnrollment(
            id=enrollment_id,
            school_id=school_id,
            student_id=student_id,
            administrative_class_id=class_id,
            student_number="20250001",
            seat_number="03",
            source_kind="manual",
            source_ref=str(uuid4()),
            valid_from=T0,
            valid_to=T3,
            status="confirmed",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )

    old = repository.resolve_student_relation_snapshot(
        SchoolId(school_id), StudentId(student_id), as_of=T0 + timedelta(days=1)
    )
    current = repository.get_current_student_relation(
        SchoolId(school_id), StudentId(student_id), as_of=T2
    )
    assert isinstance(old, StudentRelationSnapshot)
    assert old.display_name == "李明"
    assert old.administrative_class_display_name == "初一（3）班"
    assert isinstance(current, StudentRelationSnapshot)
    assert current.display_name == "李小明"
    assert current.administrative_class_display_name == "七年级（3）班"

    repository.end_enrollment(SchoolId(school_id), EnrollmentId(enrollment_id), effective_at=T2)
    assert (
        repository.get_current_student_relation(
            SchoolId(school_id), StudentId(student_id), as_of=T2
        )
        is None
    )
    assert (
        repository.resolve_student_relation_snapshot(
            SchoolId(school_id), StudentId(student_id), as_of=T1
        )
        is not None
    )


@pytest.mark.integration
def test_current_roster_uses_one_as_of_total_and_stable_fifty_row_cursor(
    current_owner_session: Session,
) -> None:
    student_models = importlib.import_module("app.domains.students.models")
    student_repository = importlib.import_module("app.domains.students.repository")
    school_id = uuid4()
    class_id = uuid4()
    current_owner_session.add(_school(school_id, code="roster"))
    current_owner_session.flush()
    person_repository = SqlAlchemyPersonRepository(current_owner_session)
    repository = student_repository.SqlAlchemyStudentRepository(
        current_owner_session,
        person_snapshot_resolver=person_repository,
    )
    repository.add_administrative_class(
        student_models.AdministrativeClass(
            id=class_id,
            school_id=school_id,
            school_year_code="2025-2026",
            grade_code="grade-7",
            status="active",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    repository.add_administrative_class_version(
        student_models.AdministrativeClassVersion(
            id=uuid4(),
            school_id=school_id,
            administrative_class_id=class_id,
            display_name="初一（3）班",
            short_name="初一3班",
            valid_from=T0,
            valid_to=None,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="confirmed",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    for index in range(51):
        person_id = uuid4()
        student_id = uuid4()
        person_repository.add_person(
            SchoolPerson(
                id=person_id,
                school_id=school_id,
                status="active",
                version=1,
                created_at=T0,
                updated_at=T0,
            )
        )
        person_repository.add_name(
            SchoolPersonName(
                id=uuid4(),
                school_id=school_id,
                person_id=person_id,
                display_name="李明" if index < 2 else f"学生{index:02d}",
                valid_from=T0,
                valid_to=None,
                source_kind="manual",
                source_ref=str(uuid4()),
                status="confirmed",
                version=1,
                created_at=T0,
                updated_at=T0,
            )
        )
        repository.add_student(
            student_models.Student(
                id=student_id,
                school_id=school_id,
                person_id=person_id,
                status="active",
                version=1,
                created_at=T0,
                updated_at=T0,
            )
        )
        repository.add_enrollment(
            student_models.StudentEnrollment(
                id=uuid4(),
                school_id=school_id,
                student_id=student_id,
                administrative_class_id=class_id,
                student_number=f"2025{index:04d}",
                seat_number=str(index + 1),
                source_kind="manual",
                source_ref=str(uuid4()),
                valid_from=T0,
                valid_to=None,
                status="confirmed",
                version=1,
                created_at=T0,
                updated_at=T0,
            )
        )

    second_class_id = uuid4()
    repository.add_administrative_class(
        student_models.AdministrativeClass(
            id=second_class_id,
            school_id=school_id,
            school_year_code="2025-2026",
            grade_code="grade-7",
            status="active",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    repository.add_administrative_class_version(
        student_models.AdministrativeClassVersion(
            id=uuid4(),
            school_id=school_id,
            administrative_class_id=second_class_id,
            display_name="初一（4）班",
            short_name="初一4班",
            valid_from=T0,
            valid_to=None,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="confirmed",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    extra_person_id = uuid4()
    extra_student_id = uuid4()
    person_repository.add_person(
        SchoolPerson(
            id=extra_person_id,
            school_id=school_id,
            status="active",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    person_repository.add_name(
        SchoolPersonName(
            id=uuid4(),
            school_id=school_id,
            person_id=extra_person_id,
            display_name="另一班学生",
            valid_from=T0,
            valid_to=None,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="confirmed",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    repository.add_student(
        student_models.Student(
            id=extra_student_id,
            school_id=school_id,
            person_id=extra_person_id,
            status="active",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    repository.add_enrollment(
        student_models.StudentEnrollment(
            id=uuid4(),
            school_id=school_id,
            student_id=extra_student_id,
            administrative_class_id=second_class_id,
            student_number="20259999",
            seat_number="01",
            source_kind="manual",
            source_ref=str(uuid4()),
            valid_from=T0,
            valid_to=None,
            status="confirmed",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )

    roster_statements: list[str] = []
    connection = current_owner_session.connection()

    def record_roster_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        roster_statements.append(statement)

    event.listen(connection, "before_cursor_execute", record_roster_statement)
    try:
        first_page = repository.get_current_class_roster(
            SchoolId(school_id), AdministrativeClassId(class_id), as_of=T1
        )
    finally:
        event.remove(connection, "before_cursor_execute", record_roster_statement)
    assert first_page.as_of == T1
    assert first_page.total_count == 51
    assert len(first_page.items) == 50
    assert first_page.next_after_student_id == first_page.items[-1].student_id
    assert len({item.student_id for item in first_page.items}) == 50
    assert all(item.disambiguation_suffix for item in first_page.items)
    assert len(roster_statements) == 2

    second_page = repository.get_current_class_roster(
        SchoolId(school_id),
        AdministrativeClassId(class_id),
        as_of=first_page.as_of,
        after_student_id=first_page.next_after_student_id,
    )
    assert second_page.total_count == 51
    assert len(second_page.items) == 1
    assert second_page.next_after_student_id is None
    assert second_page.items[0].student_id not in {item.student_id for item in first_page.items}

    empty_page = repository.get_current_class_roster(
        SchoolId(school_id),
        AdministrativeClassId(class_id),
        as_of=first_page.as_of,
        after_student_id=second_page.items[-1].student_id,
    )
    assert empty_page.total_count == 51
    assert empty_page.items == ()
    assert empty_page.next_after_student_id is None

    grade_page = repository.get_current_grade_roster(SchoolId(school_id), "grade-7", as_of=T1)
    assert grade_page.total_count == 52
    assert len(grade_page.items) == 50


@pytest.mark.integration
def test_campus_current_and_historical_readers_do_not_rewrite_old_name(
    current_owner_session: Session,
) -> None:
    campus_models = importlib.import_module("app.domains.campus.models")
    campus_repository = importlib.import_module("app.domains.campus.repository")
    school_id = uuid4()
    campus_id = uuid4()
    location_id = uuid4()
    current_owner_session.add(_school(school_id, code="campus-history"))
    current_owner_session.flush()
    repository = campus_repository.SqlAlchemyCampusRepository(current_owner_session)
    repository.add_location(
        campus_models.CampusLocation(
            id=campus_id,
            school_id=school_id,
            parent_location_id=None,
            kind="campus",
            status="active",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    location = campus_models.CampusLocation(
        id=location_id,
        school_id=school_id,
        parent_location_id=campus_id,
        kind="classroom",
        status="active",
        version=1,
        created_at=T0,
        updated_at=T0,
    )
    repository.add_location(location)
    raw_connection = current_owner_session.connection().connection.driver_connection
    assert isinstance(raw_connection, psycopg.Connection)
    _insert_location_version(
        raw_connection,
        school_id=school_id,
        location_id=campus_id,
        display_name="主校区",
        display_path="/主校区",
        valid_from=T0,
        valid_to=None,
    )
    _insert_location_version(
        raw_connection,
        school_id=school_id,
        location_id=location_id,
        display_name="301教室",
        display_path="/主校区/301教室",
        valid_from=T0,
        valid_to=T1,
    )
    _insert_location_version(
        raw_connection,
        school_id=school_id,
        location_id=location_id,
        display_name="七年级3班教室",
        display_path="/主校区/七年级3班教室",
        valid_from=T1,
        valid_to=None,
    )
    current_owner_session.expire_all()
    old = repository.resolve_location_snapshot(
        SchoolId(school_id), LocationId(location_id), as_of=T0 + timedelta(days=1)
    )
    current = repository.get_current_location(
        SchoolId(school_id), LocationId(location_id), as_of=T2
    )
    assert isinstance(old, LocationSnapshot)
    assert old.display_name == "301教室"
    assert isinstance(current, LocationSnapshot)
    assert current.display_name == "七年级3班教室"

    actor_user_id, actor_person_id, actor_assignment_id = _insert_structure_actor(
        raw_connection,
        school_id=school_id,
    )
    source_key_row = raw_connection.execute(
        "SELECT source_key FROM campus_locations WHERE school_id = %s AND id = %s",
        (school_id, location_id),
    ).fetchone()
    assert source_key_row is not None
    _seed_formal_structure_lineage(
        raw_connection,
        school_id=school_id,
        user_id=actor_user_id,
        person_id=actor_person_id,
        assignment_id=actor_assignment_id,
        fact_kind="location",
        fact_source_key=str(source_key_row[0]),
        target_id=location_id,
        effective_at=T2,
        operation="end",
        target_version=3,
        expected_version=2,
    )
    raw_connection.execute(
        "UPDATE campus_locations SET status = 'inactive', version = 3 "
        "WHERE school_id = %s AND id = %s",
        (school_id, location_id),
    )
    current_owner_session.expire_all()
    assert (
        repository.get_current_location(SchoolId(school_id), LocationId(location_id), as_of=T2)
        is None
    )
    assert (
        repository.resolve_location_snapshot(
            SchoolId(school_id),
            LocationId(location_id),
            as_of=T1 + timedelta(days=1),
        )
        is not None
    )
