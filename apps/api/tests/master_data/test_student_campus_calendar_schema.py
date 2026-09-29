from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
import threading
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg.errors import (
    CheckViolation,
    ExclusionViolation,
    UniqueViolation,
)
from sqlalchemy.orm import Session

from tests.master_data.conftest import PostgresRoles
from tests.master_data.test_core_schema import (
    _foreign_key_contract,  # pyright: ignore[reportPrivateUsage]
    _insert_account_binding,  # pyright: ignore[reportPrivateUsage]
    _insert_assignment,  # pyright: ignore[reportPrivateUsage]
    _insert_organization_unit,  # pyright: ignore[reportPrivateUsage]
    _insert_user,  # pyright: ignore[reportPrivateUsage]
)
from tests.master_data.test_execution_context import (
    _seed_formal_structure_lineage,  # pyright: ignore[reportPrivateUsage]
)

API_ROOT = Path(__file__).resolve().parents[2]
TASK4_TABLES = (
    "administrative_class_versions",
    "administrative_classes",
    "campus_location_versions",
    "campus_locations",
    "guardian_relationships",
    "school_calendar_days",
    "school_calendar_versions",
    "student_enrollments",
    "students",
)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = T0 + timedelta(days=30)
T2 = T1 + timedelta(days=30)
T3 = T2 + timedelta(days=30)


def _current_alembic_head() -> str:
    configuration = Config(str(API_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(API_ROOT / "alembic"))
    head = ScriptDirectory.from_config(configuration).get_current_head()
    assert head is not None
    return head


def _insert_school(
    connection: psycopg.Connection[Any],
    school_id: UUID,
    *,
    code: str,
) -> None:
    connection.execute(
        """
        INSERT INTO schools (id, school_code, display_name, timezone_name, status, version)
        VALUES (%s, %s, %s, 'Asia/Shanghai', 'active', 1)
        """,
        (school_id, code, f"学校-{code}"),
    )


def _insert_person(
    connection: psycopg.Connection[Any],
    school_id: UUID,
    person_id: UUID,
    *,
    display_name: str = "李明",
) -> None:
    connection.execute(
        """
        INSERT INTO school_people (id, school_id, status, version)
        VALUES (%s, %s, 'active', 1)
        """,
        (person_id, school_id),
    )
    connection.execute(
        """
        INSERT INTO school_person_names (
            id, school_id, person_id, display_name, valid_from, valid_to,
            source_kind, source_ref, status, version
        )
        VALUES (%s, %s, %s, %s, %s, NULL, 'manual', %s, 'confirmed', 1)
        """,
        (uuid4(), school_id, person_id, display_name, T0, str(uuid4())),
    )


def _insert_class(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    class_id: UUID,
    grade_code: str = "grade-7",
    display_name: str = "初一（3）班",
) -> None:
    connection.execute(
        """
        INSERT INTO administrative_classes (
            id, school_id, school_year_code, grade_code, status, version
        )
        VALUES (%s, %s, '2025-2026', %s, 'active', 1)
        """,
        (class_id, school_id, grade_code),
    )
    connection.execute(
        """
        INSERT INTO administrative_class_versions (
            id, school_id, administrative_class_id, display_name, short_name,
            valid_from, valid_to, source_kind, source_ref, status, version
        )
        VALUES (%s, %s, %s, %s, %s, %s, NULL, 'manual', %s, 'confirmed', 1)
        """,
        (
            uuid4(),
            school_id,
            class_id,
            display_name,
            display_name,
            T0,
            str(uuid4()),
        ),
    )


def _insert_student(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    student_id: UUID,
    person_id: UUID,
) -> None:
    connection.execute(
        """
        INSERT INTO students (id, school_id, person_id, status, version)
        VALUES (%s, %s, %s, 'active', 1)
        """,
        (student_id, school_id, person_id),
    )


def _insert_enrollment(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    enrollment_id: UUID,
    student_id: UUID,
    class_id: UUID,
    valid_from: datetime,
    valid_to: datetime | None,
    status: str = "confirmed",
    student_number: str | None = "20250001",
    seat_number: str | None = "03",
) -> None:
    connection.execute(
        """
        INSERT INTO student_enrollments (
            id, school_id, student_id, administrative_class_id,
            student_number, seat_number, source_kind, source_ref,
            valid_from, valid_to, status, version
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'manual', %s, %s, %s, %s, 1)
        """,
        (
            enrollment_id,
            school_id,
            student_id,
            class_id,
            student_number,
            seat_number,
            str(uuid4()),
            valid_from,
            valid_to,
            status,
        ),
    )


def _insert_location(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    location_id: UUID,
    parent_id: UUID | None = None,
    kind: str = "classroom",
) -> None:
    connection.execute(
        """
        INSERT INTO campus_locations (
            id, school_id, parent_location_id, kind, status, version
        )
        VALUES (%s, %s, %s, %s, 'active', 1)
        """,
        (location_id, school_id, parent_id, kind),
    )


def _insert_structure_actor(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
) -> tuple[int, UUID, UUID]:
    actor_user_id = _insert_user(connection)
    actor_person_id = uuid4()
    actor_unit_id = uuid4()
    _insert_person(connection, school_id, actor_person_id)
    _insert_organization_unit(
        connection,
        school_id=school_id,
        unit_id=actor_unit_id,
        kind="administration",
    )
    actor_assignment_id = _insert_assignment(
        connection,
        school_id=school_id,
        person_id=actor_person_id,
        unit_id=actor_unit_id,
        semantic_role_code="school_master_data_manager",
        valid_from=T0 - timedelta(days=1),
        valid_to=None,
    )
    _insert_account_binding(
        connection,
        school_id=school_id,
        account_user_id=actor_user_id,
        person_id=actor_person_id,
        valid_from=T0 - timedelta(days=1),
        valid_to=None,
    )
    return actor_user_id, actor_person_id, actor_assignment_id


def _insert_location_version(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    location_id: UUID,
    display_name: str,
    display_path: str,
    valid_from: datetime,
    valid_to: datetime | None,
    status: str = "confirmed",
) -> None:
    stable_row = connection.execute(
        """
        SELECT parent_location_id, kind, source_key, version
        FROM campus_locations
        WHERE school_id = %s AND id = %s
        FOR UPDATE
        """,
        (school_id, location_id),
    ).fetchone()
    assert stable_row is not None
    latest_version_row = connection.execute(
        """
        SELECT max(version)
        FROM campus_location_versions
        WHERE school_id = %s AND location_id = %s
        """,
        (school_id, location_id),
    ).fetchone()
    assert latest_version_row is not None
    latest_version = latest_version_row[0]
    target_version = int(stable_row[3]) if latest_version is None else int(latest_version) + 1
    if int(stable_row[3]) != target_version:
        connection.execute(
            """
            UPDATE campus_locations
            SET version = %s
            WHERE school_id = %s AND id = %s
            """,
            (target_version, school_id, location_id),
        )

    actor_user_id, actor_person_id, actor_assignment_id = _insert_structure_actor(
        connection,
        school_id=school_id,
    )
    batch_id = _seed_formal_structure_lineage(
        connection,
        school_id=school_id,
        user_id=actor_user_id,
        person_id=actor_person_id,
        assignment_id=actor_assignment_id,
        fact_kind="location",
        fact_source_key=str(stable_row[2]),
        target_id=location_id,
        effective_at=valid_from,
        target_version=target_version,
        expected_version=target_version - 1,
    )
    connection.execute(
        """
        INSERT INTO campus_location_versions (
            id, school_id, location_id, parent_location_id, kind,
            display_name, aliases, display_path,
            valid_from, valid_to, source_kind, source_ref, status, version
        )
        VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, 'manual', %s, %s, %s
        )
        """,
        (
            uuid4(),
            school_id,
            location_id,
            stable_row[0],
            stable_row[1],
            display_name,
            [display_name],
            display_path,
            valid_from,
            valid_to,
            f"change-set:{batch_id}",
            status,
            target_version,
        ),
    )


def _migration_environment(postgres_roles: PostgresRoles) -> dict[str, str]:
    """构造不输出测试密码的 Alembic 迁移环境。"""

    migration = postgres_roles.migration_connection_parameters
    environment = os.environ.copy()
    environment.update(
        {
            "POSTGRES_MIGRATION_HOST": str(migration["host"]),
            "POSTGRES_MIGRATION_PORT": str(migration["port"]),
            "POSTGRES_MIGRATION_DB": str(migration["dbname"]),
            "POSTGRES_MIGRATION_USER": str(migration["user"]),
            "POSTGRES_MIGRATION_PASSWORD": str(migration["password"]),
            "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY": (
                "master-data-schema-context-signing-key-32-bytes"
            ),
        }
    )
    return environment


@pytest.mark.integration
def test_task4_revision_tables_and_owner_are_exact(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    revision_row = current_owner_connection.execute(
        "SELECT version_num FROM public.alembic_version"
    ).fetchone()
    assert revision_row == (_current_alembic_head(),)

    rows = current_owner_connection.execute(
        """
        SELECT relation.relname, owner.rolname
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_roles AS owner ON owner.oid = relation.relowner
        WHERE namespace.nspname = 'public'
          AND relation.relname = ANY(%s)
        ORDER BY relation.relname
        """,
        (list(TASK4_TABLES),),
    ).fetchall()
    assert rows == [(table_name, "campus_ai_db_owner") for table_name in TASK4_TABLES]


@pytest.mark.integration
def test_same_name_students_remain_distinct_even_in_same_class(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    class_id = uuid4()
    first_person = uuid4()
    second_person = uuid4()
    first_student = uuid4()
    second_student = uuid4()
    _insert_school(current_owner_connection, school_id, code="same-name")
    _insert_class(current_owner_connection, school_id=school_id, class_id=class_id)
    _insert_person(current_owner_connection, school_id, first_person, display_name="李明")
    _insert_person(current_owner_connection, school_id, second_person, display_name="李明")
    _insert_student(
        current_owner_connection,
        school_id=school_id,
        student_id=first_student,
        person_id=first_person,
    )
    _insert_student(
        current_owner_connection,
        school_id=school_id,
        student_id=second_student,
        person_id=second_person,
    )
    _insert_enrollment(
        current_owner_connection,
        school_id=school_id,
        enrollment_id=uuid4(),
        student_id=first_student,
        class_id=class_id,
        valid_from=T0,
        valid_to=None,
        student_number="20250001",
        seat_number="03",
    )
    _insert_enrollment(
        current_owner_connection,
        school_id=school_id,
        enrollment_id=uuid4(),
        student_id=second_student,
        class_id=class_id,
        valid_from=T0,
        valid_to=None,
        student_number="20250002",
        seat_number="17",
    )

    rows = current_owner_connection.execute(
        """
        SELECT student.id, student.person_id, enrollment.seat_number
        FROM students AS student
        JOIN student_enrollments AS enrollment
          ON enrollment.school_id = student.school_id
         AND enrollment.student_id = student.id
        WHERE student.school_id = %s
        ORDER BY enrollment.seat_number
        """,
        (school_id,),
    ).fetchall()
    assert rows == [
        (first_student, first_person, "03"),
        (second_student, second_person, "17"),
    ]


@pytest.mark.integration
def test_student_profile_and_enrollment_declare_school_scoped_foreign_keys(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    assert _foreign_key_contract(
        current_owner_connection,
        table_name="students",
        constraint_name="fk_students_school_person",
    ) == (
        ("school_id", "person_id"),
        "school_people",
        ("school_id", "id"),
        "r",
    )
    assert _foreign_key_contract(
        current_owner_connection,
        table_name="student_enrollments",
        constraint_name="fk_student_enrollments_school_student",
    ) == (
        ("school_id", "student_id"),
        "students",
        ("school_id", "id"),
        "r",
    )
    assert _foreign_key_contract(
        current_owner_connection,
        table_name="student_enrollments",
        constraint_name="fk_student_enrollments_school_class",
    ) == (
        ("school_id", "administrative_class_id"),
        "administrative_classes",
        ("school_id", "id"),
        "r",
    )


@pytest.mark.integration
def test_one_person_has_at_most_one_student_profile_per_school(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    person_id = uuid4()
    _insert_school(current_owner_connection, school_id, code="one-profile")
    _insert_person(current_owner_connection, school_id, person_id)
    _insert_student(
        current_owner_connection,
        school_id=school_id,
        student_id=uuid4(),
        person_id=person_id,
    )
    with pytest.raises(UniqueViolation), current_owner_connection.transaction():
        _insert_student(
            current_owner_connection,
            school_id=school_id,
            student_id=uuid4(),
            person_id=person_id,
        )


@pytest.mark.integration
def test_confirmed_enrollment_rejects_overlap_but_allows_adjacent_and_draft(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    person_id = uuid4()
    student_id = uuid4()
    class_a = uuid4()
    class_b = uuid4()
    _insert_school(current_owner_connection, school_id, code="enrollment-period")
    _insert_person(current_owner_connection, school_id, person_id)
    _insert_class(current_owner_connection, school_id=school_id, class_id=class_a)
    _insert_class(
        current_owner_connection,
        school_id=school_id,
        class_id=class_b,
        display_name="初一（4）班",
    )
    _insert_student(
        current_owner_connection,
        school_id=school_id,
        student_id=student_id,
        person_id=person_id,
    )
    _insert_enrollment(
        current_owner_connection,
        school_id=school_id,
        enrollment_id=uuid4(),
        student_id=student_id,
        class_id=class_a,
        valid_from=T0,
        valid_to=T1,
    )

    with pytest.raises(ExclusionViolation), current_owner_connection.transaction():
        _insert_enrollment(
            current_owner_connection,
            school_id=school_id,
            enrollment_id=uuid4(),
            student_id=student_id,
            class_id=class_b,
            valid_from=T0 + timedelta(days=1),
            valid_to=T2,
        )

    _insert_enrollment(
        current_owner_connection,
        school_id=school_id,
        enrollment_id=uuid4(),
        student_id=student_id,
        class_id=class_b,
        valid_from=T0 + timedelta(days=1),
        valid_to=T2,
        status="draft",
    )
    _insert_enrollment(
        current_owner_connection,
        school_id=school_id,
        enrollment_id=uuid4(),
        student_id=student_id,
        class_id=class_b,
        valid_from=T1,
        valid_to=None,
    )


@pytest.mark.integration
def test_graduation_ends_enrollment_without_deleting_student_or_history(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    person_id = uuid4()
    student_id = uuid4()
    enrollment_id = uuid4()
    class_id = uuid4()
    _insert_school(current_owner_connection, school_id, code="graduation")
    _insert_person(current_owner_connection, school_id, person_id)
    _insert_class(current_owner_connection, school_id=school_id, class_id=class_id)
    _insert_student(
        current_owner_connection,
        school_id=school_id,
        student_id=student_id,
        person_id=person_id,
    )
    _insert_enrollment(
        current_owner_connection,
        school_id=school_id,
        enrollment_id=enrollment_id,
        student_id=student_id,
        class_id=class_id,
        valid_from=T0,
        valid_to=T1,
    )

    student_row = current_owner_connection.execute(
        "SELECT status FROM students WHERE school_id = %s AND id = %s",
        (school_id, student_id),
    ).fetchone()
    historical_row = current_owner_connection.execute(
        """
        SELECT administrative_class_id
        FROM student_enrollments
        WHERE school_id = %s AND student_id = %s
          AND valid_from <= %s AND valid_to > %s
        """,
        (school_id, student_id, T0 + timedelta(days=1), T0 + timedelta(days=1)),
    ).fetchone()
    current_row = current_owner_connection.execute(
        """
        SELECT id
        FROM student_enrollments
        WHERE school_id = %s AND student_id = %s
          AND status = 'confirmed'
          AND valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)
        """,
        (school_id, student_id, T2, T2),
    ).fetchone()
    assert student_row == ("active",)
    assert historical_row == (class_id,)
    assert current_row is None


@pytest.mark.integration
def test_class_versions_allow_homonyms_but_reject_same_class_overlap(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    class_a = uuid4()
    class_b = uuid4()
    _insert_school(current_owner_connection, school_id, code="class-version")
    _insert_class(current_owner_connection, school_id=school_id, class_id=class_a)
    _insert_class(current_owner_connection, school_id=school_id, class_id=class_b)

    with pytest.raises(ExclusionViolation), current_owner_connection.transaction():
        current_owner_connection.execute(
            """
            INSERT INTO administrative_class_versions (
                id, school_id, administrative_class_id, display_name, short_name,
                valid_from, valid_to, source_kind, source_ref, status, version
            )
            VALUES (%s, %s, %s, '七年级三班', '七3', %s, NULL,
                    'manual', %s, 'confirmed', 2)
            """,
            (uuid4(), school_id, class_a, T1, str(uuid4())),
        )


@pytest.mark.integration
def test_guardian_relationship_declares_school_scoped_foreign_keys_and_rejects_overlap(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    assert _foreign_key_contract(
        current_owner_connection,
        table_name="guardian_relationships",
        constraint_name="fk_guardian_relationships_school_person",
    ) == (
        ("school_id", "guardian_person_id"),
        "school_people",
        ("school_id", "id"),
        "r",
    )
    assert _foreign_key_contract(
        current_owner_connection,
        table_name="guardian_relationships",
        constraint_name="fk_guardian_relationships_school_student",
    ) == (
        ("school_id", "student_id"),
        "students",
        ("school_id", "id"),
        "r",
    )

    school_id = uuid4()
    guardian_id = uuid4()
    student_person_id = uuid4()
    student_id = uuid4()
    _insert_school(current_owner_connection, school_id, code="guardian")
    _insert_person(current_owner_connection, school_id, guardian_id, display_name="家长甲")
    _insert_person(current_owner_connection, school_id, student_person_id)
    _insert_student(
        current_owner_connection,
        school_id=school_id,
        student_id=student_id,
        person_id=student_person_id,
    )

    current_owner_connection.execute(
        """
        INSERT INTO guardian_relationships (
            id, school_id, guardian_person_id, student_id, relationship_kind,
            verification_source, verification_reference, verified_at,
            valid_from, valid_to, status, version
        )
        VALUES (%s, %s, %s, %s, 'parent', 'manual', %s, %s,
                %s, NULL, 'confirmed', 1)
        """,
        (uuid4(), school_id, guardian_id, student_id, str(uuid4()), T0, T0),
    )
    with pytest.raises(ExclusionViolation), current_owner_connection.transaction():
        current_owner_connection.execute(
            """
            INSERT INTO guardian_relationships (
                id, school_id, guardian_person_id, student_id, relationship_kind,
                verification_source, verification_reference, verified_at,
                valid_from, valid_to, status, version
            )
            VALUES (%s, %s, %s, %s, 'parent', 'manual', %s, %s,
                    %s, NULL, 'confirmed', 2)
            """,
            (uuid4(), school_id, guardian_id, student_id, str(uuid4()), T1, T1),
        )


@pytest.mark.integration
def test_campus_parent_declares_school_scoped_fk_and_has_no_responsibility_columns(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    assert _foreign_key_contract(
        current_owner_connection,
        table_name="campus_locations",
        constraint_name="fk_campus_locations_school_parent",
    ) == (
        ("school_id", "parent_location_id"),
        "campus_locations",
        ("school_id", "id"),
        "r",
    )

    columns = {
        row[0]
        for row in current_owner_connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'campus_locations'
            """
        ).fetchall()
    }
    assert columns.isdisjoint(
        {
            "person_id",
            "assignment_id",
            "responsibility_group_id",
            "asset_manager_id",
            "repair_worker_id",
        }
    )


@pytest.mark.integration
def test_only_top_level_campus_can_omit_parent(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    campus_id = uuid4()
    _insert_school(current_owner_connection, school_id, code="location-root")

    with pytest.raises(CheckViolation), current_owner_connection.transaction():
        _insert_location(
            current_owner_connection,
            school_id=school_id,
            location_id=uuid4(),
            kind="building",
        )

    _insert_location(
        current_owner_connection,
        school_id=school_id,
        location_id=campus_id,
        kind="campus",
    )
    _insert_location(
        current_owner_connection,
        school_id=school_id,
        location_id=uuid4(),
        parent_id=campus_id,
        kind="building",
    )

    with pytest.raises(CheckViolation), current_owner_connection.transaction():
        _insert_location(
            current_owner_connection,
            school_id=school_id,
            location_id=uuid4(),
            parent_id=campus_id,
            kind="campus",
        )


@pytest.mark.integration
def test_campus_rejects_self_parent_and_overlapping_confirmed_display_versions(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    location_id = uuid4()
    _insert_school(current_owner_connection, school_id, code="location-period")
    with pytest.raises(CheckViolation), current_owner_connection.transaction():
        _insert_location(
            current_owner_connection,
            school_id=school_id,
            location_id=location_id,
            parent_id=location_id,
        )

    _insert_location(
        current_owner_connection,
        school_id=school_id,
        location_id=location_id,
        kind="campus",
    )
    _insert_location_version(
        current_owner_connection,
        school_id=school_id,
        location_id=location_id,
        display_name="301教室",
        display_path="/教学楼/3楼/301教室",
        valid_from=T0,
        valid_to=None,
    )
    with pytest.raises(ExclusionViolation), current_owner_connection.transaction():
        _insert_location_version(
            current_owner_connection,
            school_id=school_id,
            location_id=location_id,
            display_name="三零一教室",
            display_path="/教学楼/3楼/三零一教室",
            valid_from=T1,
            valid_to=None,
        )


@pytest.mark.integration
def test_campus_rejects_indirect_parent_cycle(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    campus_id = uuid4()
    parent_id = uuid4()
    child_id = uuid4()
    _insert_school(current_owner_connection, school_id, code="location-cycle")
    _insert_location(
        current_owner_connection,
        school_id=school_id,
        location_id=campus_id,
        kind="campus",
    )
    _insert_location(
        current_owner_connection,
        school_id=school_id,
        location_id=parent_id,
        parent_id=campus_id,
        kind="building",
    )
    _insert_location(
        current_owner_connection,
        school_id=school_id,
        location_id=child_id,
        parent_id=parent_id,
        kind="floor",
    )

    with pytest.raises(CheckViolation), current_owner_connection.transaction():
        current_owner_connection.execute(
            """
            UPDATE campus_locations
            SET parent_location_id = %s
            WHERE school_id = %s AND id = %s
            """,
            (child_id, school_id, parent_id),
        )


@pytest.mark.integration
def test_concurrent_parent_updates_cannot_commit_a_two_node_cycle(
    isolated_migration_schema: PostgresRoles,
) -> None:
    school_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    with isolated_migration_schema.migration_connection() as setup:
        setup.autocommit = False
        setup.execute("SET ROLE campus_ai_db_owner")
        _insert_school(setup, school_id, code="location-concurrency")
        _insert_location(
            setup,
            school_id=school_id,
            location_id=first_id,
            kind="campus",
        )
        _insert_location(
            setup,
            school_id=school_id,
            location_id=second_id,
            kind="campus",
        )
        _insert_location_version(
            setup,
            school_id=school_id,
            location_id=first_id,
            display_name="第一校区",
            display_path="/第一校区",
            valid_from=T0,
            valid_to=None,
        )
        _insert_location_version(
            setup,
            school_id=school_id,
            location_id=second_id,
            display_name="第二校区",
            display_path="/第二校区",
            valid_from=T0,
            valid_to=None,
        )
        first_actor = _insert_structure_actor(setup, school_id=school_id)
        second_actor = _insert_structure_actor(setup, school_id=school_id)

    updates_started = threading.Barrier(2)
    outcomes: list[str] = []
    outcome_lock = threading.Lock()

    def update_parent(
        location_id: UUID,
        parent_id: UUID,
        actor: tuple[int, UUID, UUID],
    ) -> None:
        connection = psycopg.connect(
            **isolated_migration_schema.migration_connection_parameters,
            autocommit=False,
        )
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute("SET LOCAL lock_timeout = '5s'")
        try:
            updates_started.wait(timeout=5)
            connection.execute("SET CONSTRAINTS trg_campus_locations_acyclic DEFERRED")
            connection.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended(
                        'campus-ai:sf18:location-structure:' || CAST(%s AS text),
                        0
                    )
                )
                """,
                (school_id,),
            )
            stable_row = connection.execute(
                """
                SELECT source_key, version
                FROM campus_locations
                WHERE school_id = %s AND id = %s
                FOR UPDATE
                """,
                (school_id, location_id),
            ).fetchone()
            assert stable_row is not None
            assert stable_row[1] == 1
            batch_id = _seed_formal_structure_lineage(
                connection,
                school_id=school_id,
                user_id=actor[0],
                person_id=actor[1],
                assignment_id=actor[2],
                fact_kind="location",
                fact_source_key=str(stable_row[0]),
                target_id=location_id,
                effective_at=T1,
                target_version=2,
                expected_version=1,
            )
            connection.execute(
                """
                UPDATE campus_locations
                SET kind = 'building', parent_location_id = %s, version = 2
                WHERE school_id = %s AND id = %s
                """,
                (parent_id, school_id, location_id),
            )
            connection.execute(
                """
                UPDATE campus_location_versions
                SET valid_to = %s
                WHERE school_id = %s AND location_id = %s
                  AND version = 1 AND valid_to IS NULL
                """,
                (T1, school_id, location_id),
            )
            connection.execute(
                """
                INSERT INTO campus_location_versions (
                    id, school_id, location_id, parent_location_id, kind,
                    display_name, aliases, display_path,
                    valid_from, valid_to, source_kind, source_ref, status, version
                ) VALUES (
                    %s, %s, %s, %s, 'building',
                    %s, %s, %s,
                    %s, NULL, 'manual', %s, 'confirmed', 2
                )
                """,
                (
                    uuid4(),
                    school_id,
                    location_id,
                    parent_id,
                    f"楼栋-{location_id}",
                    [f"楼栋-{location_id}"],
                    f"/楼栋-{location_id}",
                    T1,
                    f"change-set:{batch_id}",
                ),
            )
            connection.commit()
            outcome = "committed"
        except CheckViolation:
            connection.rollback()
            outcome = "rejected"
        finally:
            connection.close()
        with outcome_lock:
            outcomes.append(outcome)

    first = threading.Thread(target=update_parent, args=(first_id, second_id, first_actor))
    second = threading.Thread(target=update_parent, args=(second_id, first_id, second_actor))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(outcomes) == ["committed", "rejected"]

    with isolated_migration_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        rows = connection.execute(
            """
            SELECT id, parent_location_id
            FROM campus_locations
            WHERE school_id = %s
            ORDER BY id
            """,
            (school_id,),
        ).fetchall()
        assert not (len(rows) == 2 and rows[0][1] == rows[1][0] and rows[1][1] == rows[0][0])


@pytest.mark.integration
def test_calendar_day_can_express_makeup_class_and_temporary_holiday(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    calendar_id = uuid4()
    _insert_school(current_owner_connection, school_id, code="calendar-day")
    current_owner_connection.execute(
        """
        INSERT INTO school_calendar_versions (
            id, school_id, display_name, timezone_name, valid_from, valid_to,
            source_kind, source_ref, status, version
        )
        VALUES (%s, %s, '2026校历', 'Asia/Shanghai', %s, NULL,
                'manual', %s, 'published', 1)
        """,
        (calendar_id, school_id, T0, str(uuid4())),
    )
    current_owner_connection.execute(
        """
        INSERT INTO school_calendar_days (
            id, school_id, calendar_version_id, local_date, day_kind,
            actual_start_time, actual_end_time, source_kind, note, version
        )
        VALUES
            (%s, %s, %s, %s, 'working', %s, %s, 'makeup_class', '周六补课', 1),
            (%s, %s, %s, %s, 'rest', NULL, NULL, 'temporary_holiday', '突发放假', 1)
        """,
        (
            uuid4(),
            school_id,
            calendar_id,
            date(2026, 2, 7),
            time(8, 0),
            time(17, 30),
            uuid4(),
            school_id,
            calendar_id,
            date(2026, 2, 9),
        ),
    )
    rows = current_owner_connection.execute(
        """
        SELECT local_date, day_kind, source_kind
        FROM school_calendar_days
        WHERE school_id = %s AND calendar_version_id = %s
        ORDER BY local_date
        """,
        (school_id, calendar_id),
    ).fetchall()
    assert rows == [
        (date(2026, 2, 7), "working", "makeup_class"),
        (date(2026, 2, 9), "rest", "temporary_holiday"),
    ]


@pytest.mark.integration
def test_published_calendar_rejects_overlap_but_allows_draft_and_adjacent_periods(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    _insert_school(current_owner_connection, school_id, code="calendar-period")
    current_owner_connection.execute(
        """
        INSERT INTO school_calendar_versions (
            id, school_id, display_name, timezone_name, valid_from, valid_to,
            source_kind, source_ref, status, version
        )
        VALUES (%s, %s, '第一版', 'Asia/Shanghai', %s, %s,
                'manual', %s, 'published', 1)
        """,
        (uuid4(), school_id, T0, T1, str(uuid4())),
    )
    with pytest.raises(ExclusionViolation), current_owner_connection.transaction():
        current_owner_connection.execute(
            """
            INSERT INTO school_calendar_versions (
                id, school_id, display_name, timezone_name, valid_from, valid_to,
                source_kind, source_ref, status, version
            )
            VALUES (%s, %s, '冲突版', 'Asia/Shanghai', %s, %s,
                    'manual', %s, 'published', 2)
            """,
            (uuid4(), school_id, T0 + timedelta(days=1), T2, str(uuid4())),
        )
    current_owner_connection.execute(
        """
        INSERT INTO school_calendar_versions (
            id, school_id, display_name, timezone_name, valid_from, valid_to,
            source_kind, source_ref, status, version
        )
        VALUES
            (%s, %s, '草稿版', 'Asia/Shanghai', %s, NULL,
             'manual', %s, 'draft', 2),
            (%s, %s, '第二版', 'Asia/Shanghai', %s, NULL,
             'manual', %s, 'published', 3)
        """,
        (
            uuid4(),
            school_id,
            T0,
            str(uuid4()),
            uuid4(),
            school_id,
            T1,
            str(uuid4()),
        ),
    )


@pytest.mark.integration
def test_calendar_day_declares_school_scoped_fk_and_rejects_duplicate_and_invalid_time(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    assert _foreign_key_contract(
        current_owner_connection,
        table_name="school_calendar_days",
        constraint_name="fk_school_calendar_days_school_version",
    ) == (
        ("school_id", "calendar_version_id"),
        "school_calendar_versions",
        ("school_id", "id"),
        "r",
    )

    school_id = uuid4()
    calendar_id = uuid4()
    _insert_school(current_owner_connection, school_id, code="calendar-check")
    current_owner_connection.execute(
        """
        INSERT INTO school_calendar_versions (
            id, school_id, display_name, timezone_name, valid_from, valid_to,
            source_kind, source_ref, status, version
        )
        VALUES (%s, %s, '校历', 'Asia/Shanghai', %s, NULL,
                'manual', %s, 'published', 1)
        """,
        (calendar_id, school_id, T0, str(uuid4())),
    )

    with pytest.raises(CheckViolation), current_owner_connection.transaction():
        current_owner_connection.execute(
            """
            INSERT INTO school_calendar_days (
                id, school_id, calendar_version_id, local_date, day_kind,
                actual_start_time, actual_end_time, source_kind, note, version
            )
            VALUES (%s, %s, %s, %s, 'working', %s, %s,
                    'makeup_class', NULL, 1)
            """,
            (
                uuid4(),
                school_id,
                calendar_id,
                date(2026, 2, 7),
                time(17, 30),
                time(8, 0),
            ),
        )

    current_owner_connection.execute(
        """
        INSERT INTO school_calendar_days (
            id, school_id, calendar_version_id, local_date, day_kind,
            actual_start_time, actual_end_time, source_kind, note, version
        )
        VALUES (%s, %s, %s, %s, 'rest', NULL, NULL,
                'temporary_holiday', NULL, 1)
        """,
        (uuid4(), school_id, calendar_id, date(2026, 2, 9)),
    )
    with pytest.raises(UniqueViolation), current_owner_connection.transaction():
        current_owner_connection.execute(
            """
            INSERT INTO school_calendar_days (
                id, school_id, calendar_version_id, local_date, day_kind,
                actual_start_time, actual_end_time, source_kind, note, version
            )
            VALUES (%s, %s, %s, %s, 'working', %s, %s,
                    'makeup_class', NULL, 1)
            """,
            (
                uuid4(),
                school_id,
                calendar_id,
                date(2026, 2, 9),
                time(8, 0),
                time(17, 30),
            ),
        )


def test_task4_repositories_do_not_own_transaction_or_session_lifecycle() -> None:
    forbidden_calls = {
        "begin",
        "close",
        "commit",
        "create_engine",
        "rollback",
        "sessionmaker",
    }
    for module_name in (
        "app.domains.students.repository",
        "app.domains.campus.repository",
        "app.platform.calendar.repository",
    ):
        module = importlib.import_module(module_name)
        module_path = Path(module.__file__ or "").resolve()
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        called_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert forbidden_calls.isdisjoint(called_attributes | called_names), module_name


def test_task4_owner_repositories_import_only_their_private_models() -> None:
    owner_roots = {
        "app.domains.students.repository": "app.domains.students.models",
        "app.domains.campus.repository": "app.domains.campus.models",
        "app.platform.calendar.repository": "app.platform.calendar.models",
    }
    for module_name, own_models in owner_roots.items():
        module = importlib.import_module(module_name)
        module_path = Path(module.__file__ or "").resolve()
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith(".models")
        }
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name.endswith(".models")
        )
        assert imported <= {own_models}, module_name


def test_task4_repository_protocols_are_importable() -> None:
    for module_name in (
        "app.domains.students.ports",
        "app.domains.campus.ports",
        "app.platform.calendar.ports",
    ):
        assert importlib.import_module(module_name) is not None


def test_task4_model_metadata_keeps_student_name_and_campus_responsibility_out() -> None:
    student_models = importlib.import_module("app.domains.students.models")
    campus_models = importlib.import_module("app.domains.campus.models")
    assert "display_name" not in student_models.Student.__table__.columns
    assert "student_name" not in student_models.Student.__table__.columns
    assert {
        "person_id",
        "assignment_id",
        "responsibility_group_id",
    }.isdisjoint(campus_models.CampusLocation.__table__.columns.keys())


def test_task4_repository_accepts_caller_owned_session(
    current_owner_session: Session,
) -> None:
    student_repository = importlib.import_module("app.domains.students.repository")
    campus_repository = importlib.import_module("app.domains.campus.repository")
    calendar_repository = importlib.import_module("app.platform.calendar.repository")
    assert student_repository.SqlAlchemyStudentRepository(current_owner_session) is not None
    assert campus_repository.SqlAlchemyCampusRepository(current_owner_session) is not None
    assert calendar_repository.SqlAlchemyWorkCalendarRepository(current_owner_session) is not None
    assert current_owner_session.in_transaction() is False


@pytest.mark.integration
def test_task4_schema_fixture_is_real_postgres_16(
    current_master_data_schema: PostgresRoles,
) -> None:
    assert 160_000 <= current_master_data_schema.server_version_num < 170_000


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_task4_migration_downgrade_and_upgrade_round_trip(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    current_master_data_schema = isolated_revision19_schema
    environment = _migration_environment(current_master_data_schema)
    downgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "20260805_04"],
        cwd=API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr

    try:
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            remaining_tables = connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = ANY(%s)
                ORDER BY table_name
                """,
                (list(TASK4_TABLES),),
            ).fetchall()
            revision_row = connection.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()
        assert remaining_tables == []
        assert revision_row == ("20260805_04",)
    finally:
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=API_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        restored_tables = connection.execute(
            """
            SELECT relation.relname, owner.rolname
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            JOIN pg_roles AS owner ON owner.oid = relation.relowner
            WHERE namespace.nspname = 'public' AND relation.relname = ANY(%s)
            ORDER BY relation.relname
            """,
            (list(TASK4_TABLES),),
        ).fetchall()
        revision_row = connection.execute(
            "SELECT version_num FROM public.alembic_version"
        ).fetchone()
    assert restored_tables == [(table_name, "campus_ai_db_owner") for table_name in TASK4_TABLES]
    assert revision_row == (_current_alembic_head(),)
