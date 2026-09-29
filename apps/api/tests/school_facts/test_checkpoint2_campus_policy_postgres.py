from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import Table, select, text
from sqlalchemy.exc import DBAPIError

from app.db.base import Base
from app.domains.students import models as student_models  # noqa: F401
from app.platform.contracts import SchoolId
from app.platform.school_facts.application import SchoolFactChangeRejected
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_checkpoint2_campus_policy_semantics import (
    _local_reference_columns,
    _organization_campus_relation,
    _policy_scope_relation,
)
from tests.school_facts.test_checkpoint2_lifecycle_red import _plan
from tests.school_facts.test_checkpoint2_residence_red import _residence_harness
from tests.school_facts.test_sf1_postgres_constraints import (
    _insert_confirmed_location_slices,
    _insert_formal_location_units,
    _LocationSliceSeed,
)

NOW = datetime(2026, 8, 21, 8, tzinfo=UTC)


@pytest.fixture
def isolated_owner_connection(
    isolated_migration_schema: PostgresRoles,
):
    """Open an owner transaction against this test's freshly migrated disposable DB."""

    with psycopg.connect(
        **isolated_migration_schema.migration_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        yield connection
        connection.rollback()


@dataclass(frozen=True)
class _ForeignKeyShape:
    table_name: str
    columns_by_target: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class _CampusTargetCase:
    code: str
    table_name: str
    target_column: str
    base_values: dict[str, object]


@dataclass(frozen=True, slots=True)
class _OrganizationSliceSeed:
    organization_id: UUID
    source_key: str
    parent_organization_id: UUID | None
    business_code: str
    display_name: str
    display_path: str
    effective_at: datetime
    target_version: int = 1
    expected_version: int = 0


def _insert_confirmed_organization_slices(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    slices: tuple[_OrganizationSliceSeed, ...],
) -> None:
    batch_id = _insert_formal_location_units(
        connection,
        school_id=school_id,
        slices=tuple(
            _LocationSliceSeed(
                location_id=item.organization_id,
                source_key=item.source_key,
                parent_location_id=item.parent_organization_id,
                kind="organization",
                business_code=item.business_code,
                display_name=item.display_name,
                aliases=(),
                display_path=item.display_path,
                effective_at=item.effective_at,
                target_version=item.target_version,
                expected_version=item.expected_version,
            )
            for item in slices
        ),
        fact_kind="organization",
    )
    for item in slices:
        connection.execute(
            """
            INSERT INTO organization_unit_versions (
                id, school_id, organization_unit_id, parent_organization_unit_id,
                business_code, display_name, display_path, valid_from, valid_to,
                source_kind, source_ref, status, version
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, NULL,
                'manual', %s, 'confirmed', %s
            )
            """,
            (
                uuid4(),
                school_id,
                item.organization_id,
                item.parent_organization_id,
                item.business_code,
                item.display_name,
                item.display_path,
                item.effective_at,
                f"change-set:{batch_id}",
                item.target_version,
            ),
        )


def _run_competing_owner_transactions(
    roles: PostgresRoles,
    *,
    first_action: Callable[[psycopg.Connection[Any]], None],
    second_action: Callable[[psycopg.Connection[Any]], None],
    label: str,
) -> tuple[tuple[str, str], tuple[str, str] | None]:
    """Hold the first invariant write while observing where the second serializes."""

    first_ready = Event()
    second_started = Event()
    second_statement_finished = Event()
    release_first = Event()
    # PostgreSQL truncates application_name to NAMEDATALEN - 1 bytes. Keep the
    # observer key comfortably below that limit so a long parametrized label
    # cannot make a real lock invisible to this harness.
    second_application_name = f"cp3-{label[:20]}-{uuid4().hex[:12]}"

    def run_first() -> str:
        connection = psycopg.connect(
            **roles.migration_connection_parameters,
            autocommit=False,
            application_name=f"{second_application_name}-first",
        )
        connection.execute("SET ROLE campus_ai_db_owner")
        try:
            first_action(connection)
            first_ready.set()
            assert release_first.wait(timeout=20), "concurrency test did not release first writer"
            connection.commit()
            return "committed"
        except psycopg.Error as exc:
            connection.rollback()
            sqlstate = str(exc.sqlstate or type(exc).__name__)
            if sqlstate == "23514":
                return sqlstate
            return f"{sqlstate}:{exc.diag.message_primary or str(exc)}"
        finally:
            first_ready.set()
            connection.close()

    def run_second() -> str:
        assert first_ready.wait(timeout=20), "first invariant writer did not reach hold point"
        connection = psycopg.connect(
            **roles.migration_connection_parameters,
            autocommit=False,
            application_name=second_application_name,
        )
        connection.execute("SET ROLE campus_ai_db_owner")
        second_started.set()
        try:
            second_action(connection)
            second_statement_finished.set()
            connection.commit()
            return "committed"
        except psycopg.Error as exc:
            connection.rollback()
            sqlstate = str(exc.sqlstate or type(exc).__name__)
            if sqlstate == "23514":
                return sqlstate
            return f"{sqlstate}:{exc.diag.message_primary or str(exc)}"
        finally:
            second_statement_finished.set()
            connection.close()

    serialization_wait: tuple[str, str] | None = None
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(run_first)
        assert first_ready.wait(timeout=20)
        second_future = executor.submit(run_second)
        assert second_started.wait(timeout=20)
        deadline = monotonic() + 10
        try:
            with psycopg.connect(
                **roles.admin_connection_parameters,
                autocommit=True,
            ) as observer:
                while monotonic() < deadline:
                    if second_statement_finished.is_set() or second_future.done():
                        break
                    row = observer.execute(
                        "SELECT wait_event_type, wait_event FROM pg_catalog.pg_stat_activity "
                        "WHERE application_name = %s",
                        (second_application_name,),
                    ).fetchone()
                    if row is not None and row[0] == "Lock":
                        serialization_wait = (str(row[0]), str(row[1]))
                        break
                    sleep(0.02)
            assert second_statement_finished.is_set() or serialization_wait is not None, (
                "second invariant writer neither completed nor reached a database lock"
            )
        finally:
            release_first.set()
        outcomes = (first_future.result(timeout=20), second_future.result(timeout=20))
    return outcomes, serialization_wait


def _foreign_key_shapes(
    connection: psycopg.Connection[Any],
) -> tuple[_ForeignKeyShape, ...]:
    rows = connection.execute(
        """
        SELECT source.relname, local_column.attname, target.relname, target_column.attname
        FROM pg_catalog.pg_constraint AS constraint_row
        JOIN pg_catalog.pg_class AS source
          ON source.oid = constraint_row.conrelid
        JOIN pg_catalog.pg_namespace AS source_namespace
          ON source_namespace.oid = source.relnamespace
        JOIN pg_catalog.pg_class AS target
          ON target.oid = constraint_row.confrelid
        JOIN LATERAL unnest(constraint_row.conkey, constraint_row.confkey)
          AS key_pair(local_number, target_number) ON TRUE
        JOIN pg_catalog.pg_attribute AS local_column
          ON local_column.attrelid = source.oid
         AND local_column.attnum = key_pair.local_number
        JOIN pg_catalog.pg_attribute AS target_column
          ON target_column.attrelid = target.oid
         AND target_column.attnum = key_pair.target_number
        WHERE constraint_row.contype = 'f'
          AND source_namespace.nspname = current_schema()
        ORDER BY source.relname, target.relname, local_column.attname
        """
    ).fetchall()
    grouped: dict[str, dict[str, set[str]]] = {}
    for table_name, local_column, target_table, target_column in rows:
        if target_column != "id" or local_column == "school_id":
            continue
        grouped.setdefault(table_name, {}).setdefault(target_table, set()).add(local_column)
    return tuple(
        _ForeignKeyShape(
            table_name=table_name,
            columns_by_target={
                target: tuple(sorted(columns)) for target, columns in targets.items()
            },
        )
        for table_name, targets in grouped.items()
    )


def _column_names(
    connection: psycopg.Connection[Any],
    table_name: str,
) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s
            """,
            (table_name,),
        )
    }


def _single_column(shape: _ForeignKeyShape, target_table: str) -> str:
    columns = shape.columns_by_target.get(target_table, ())
    assert len(columns) == 1, (
        f"{shape.table_name} 必须以一个稳定 ID 列引用 {target_table}；实际为 {columns}"
    )
    return columns[0]


def _organization_relation_shape(
    connection: psycopg.Connection[Any],
    shapes: tuple[_ForeignKeyShape, ...],
) -> _ForeignKeyShape:
    candidates = [
        shape
        for shape in shapes
        if {"organization_units", "campus_locations"} <= set(shape.columns_by_target)
        and {"school_policies", "school_policy_versions"}.isdisjoint(shape.columns_by_target)
        and shape.table_name != "official_contact_points"
        and {"valid_from", "valid_to"} <= _column_names(connection, shape.table_name)
    ]
    if len(candidates) != 1:
        pytest.fail(
            "当前 PostgreSQL head 尚无唯一的组织—校区显式有效期关系；"
            f"候选表为 {[shape.table_name for shape in candidates]}"
        )
    return candidates[0]


def _policy_scope_shape(
    connection: psycopg.Connection[Any],
    shapes: tuple[_ForeignKeyShape, ...],
) -> _ForeignKeyShape:
    candidates = [
        shape
        for shape in shapes
        if {"organization_units", "campus_locations"} <= set(shape.columns_by_target)
        and {"school_policies", "school_policy_versions"} & set(shape.columns_by_target)
    ]
    if len(candidates) != 1:
        pytest.fail(
            "当前 PostgreSQL head 尚无唯一的制度适用范围关系；"
            f"候选表为 {[shape.table_name for shape in candidates]}"
        )
    return candidates[0]


def _student_relationship_shape(
    shapes: tuple[_ForeignKeyShape, ...],
) -> _ForeignKeyShape:
    shape = next(
        (
            candidate
            for candidate in shapes
            if candidate.table_name == "student_school_relationships"
        ),
        None,
    )
    if shape is None:
        pytest.fail("PostgreSQL head 缺少 student_school_relationships")
    campus_columns = shape.columns_by_target.get("campus_locations", ())
    if len(campus_columns) != 2:
        pytest.fail(
            "student_school_relationships 尚未以两个稳定 ID 引用主要/行政校区；"
            f"实际列为 {campus_columns}"
        )
    primary = tuple(name for name in campus_columns if "primary" in name)
    administrative = tuple(name for name in campus_columns if "administrative" in name)
    if len(primary) != 1 or len(administrative) != 1:
        pytest.fail(
            "student_school_relationships 的两个校区目标尚不能机械区分主要与行政校区；"
            f"实际列为 {campus_columns}"
        )
    return shape


def _policy_target_table(shape: _ForeignKeyShape) -> str:
    targets = tuple(
        table_name
        for table_name in ("school_policy_versions", "school_policies")
        if table_name in shape.columns_by_target
    )
    assert len(targets) == 1, (
        f"{shape.table_name} 必须且只能以一个制度稳定身份作为适用范围主体；实际为 {targets}"
    )
    return targets[0]


def _insert_school_fact_parents(
    connection: psycopg.Connection[Any],
    *,
    code: str,
    include_location_slices: bool = True,
    include_initial_organization_parent: bool = False,
) -> dict[str, UUID]:
    ids = {
        "school_id": uuid4(),
        "source_id": uuid4(),
        "campus_id": uuid4(),
        "building_id": uuid4(),
        "organization_id": uuid4(),
        "second_organization_id": uuid4(),
        "student_id": uuid4(),
        "second_student_id": uuid4(),
        "policy_id": uuid4(),
        "second_policy_id": uuid4(),
        "policy_version_id": uuid4(),
        "second_policy_version_id": uuid4(),
        "third_policy_id": uuid4(),
        "third_policy_version_id": uuid4(),
        "fourth_policy_id": uuid4(),
        "fourth_policy_version_id": uuid4(),
    }
    connection.execute(
        """
        INSERT INTO schools (
            id, source_key, school_code, display_name, timezone_name, status, version
        ) VALUES (%s, %s, %s, %s, 'Asia/Shanghai', 'active', 1)
        """,
        (ids["school_id"], f"cp2-pg:{code}", f"CP2-{code}", f"CP2 {code}"),
    )
    connection.execute(
        """
        INSERT INTO school_fact_sources (
            id, school_id, source_key, source_kind, source_ref, schema_version
        ) VALUES (%s, %s, %s, 'system', %s, 1)
        """,
        (
            ids["source_id"],
            ids["school_id"],
            f"cp2-pg-source:{code}",
            f"cp2-pg:{code}",
        ),
    )
    connection.execute(
        """
        INSERT INTO campus_locations (
            id, school_id, source_key, parent_location_id, kind, status, version
        ) VALUES
          (%s, %s, %s, NULL, 'campus', 'active', 1),
          (%s, %s, %s, %s, 'building', 'active', 1)
        """,
        (
            ids["campus_id"],
            ids["school_id"],
            f"cp2-pg-campus:{code}",
            ids["building_id"],
            ids["school_id"],
            f"cp2-pg-building:{code}",
            ids["campus_id"],
        ),
    )
    if include_location_slices:
        _insert_confirmed_location_slices(
            connection,
            school_id=ids["school_id"],
            slices=(
                _LocationSliceSeed(
                    location_id=ids["campus_id"],
                    source_key=f"cp2-pg-campus:{code}",
                    parent_location_id=None,
                    kind="campus",
                    business_code="CAMPUS",
                    display_name="校区",
                    aliases=(),
                    display_path="/校区",
                    effective_at=NOW,
                ),
                _LocationSliceSeed(
                    location_id=ids["building_id"],
                    source_key=f"cp2-pg-building:{code}",
                    parent_location_id=ids["campus_id"],
                    kind="building",
                    business_code="BUILDING",
                    display_name="楼栋",
                    aliases=(),
                    display_path="/校区/楼栋",
                    effective_at=NOW,
                ),
            ),
        )
    for key in ("organization_id", "second_organization_id"):
        parent_organization_id = (
            ids["organization_id"]
            if include_initial_organization_parent and key == "second_organization_id"
            else None
        )
        connection.execute(
            """
            INSERT INTO organization_units (
                id, school_id, source_key, parent_organization_unit_id,
                kind, status, version
            ) VALUES (%s, %s, %s, %s, 'schoolwide', 'active', 1)
            """,
            (
                ids[key],
                ids["school_id"],
                f"cp2-pg-{key}:{code}",
                parent_organization_id,
            ),
        )
    for ordinal, (student_key, person_key) in enumerate(
        (("student_id", "person_id"), ("second_student_id", "second_person_id")),
        start=1,
    ):
        ids[person_key] = uuid4()
        connection.execute(
            """
            INSERT INTO school_people (id, school_id, source_key, status, version)
            VALUES (%s, %s, %s, 'active', 1)
            """,
            (ids[person_key], ids["school_id"], f"cp2-pg-person-{ordinal}:{code}"),
        )
        connection.execute(
            """
            INSERT INTO students (
                id, school_id, person_id, source_key, status, version
            ) VALUES (%s, %s, %s, %s, 'active', 1)
            """,
            (
                ids[student_key],
                ids["school_id"],
                ids[person_key],
                f"cp2-pg-student-{ordinal}:{code}",
            ),
        )
    policy_parents = (
        (ids["policy_id"], ids["policy_version_id"]),
        (ids["second_policy_id"], ids["second_policy_version_id"]),
        (ids["third_policy_id"], ids["third_policy_version_id"]),
        (ids["fourth_policy_id"], ids["fourth_policy_version_id"]),
    )
    for ordinal, (policy_id, policy_version_id) in enumerate(
        policy_parents,
        start=1,
    ):
        connection.execute(
            """
            INSERT INTO school_policies (id, school_id, source_key)
            VALUES (%s, %s, %s)
            """,
            (
                policy_id,
                ids["school_id"],
                f"cp2-pg-policy-{ordinal}:{code}",
            ),
        )
        connection.execute(
            """
            INSERT INTO school_policy_versions (
                id, school_id, policy_id, business_code, title, category_code,
                classification, publishing_organization_unit_id, published_at,
                effective_from, effective_to, supersedes_policy_version_id,
                version, source_id, import_batch_id
            ) VALUES (
                %s, %s, %s, %s, %s, 'attendance', 'public', NULL, %s,
                %s, NULL, NULL, 1, %s, NULL
            )
            """,
            (
                policy_version_id,
                ids["school_id"],
                policy_id,
                f"CP2-POLICY-{ordinal}-{code}",
                f"CP2 policy {ordinal} {code}",
                NOW,
                NOW,
                ids["source_id"],
            ),
        )
    return ids


def _required_insert_values(
    connection: psycopg.Connection[Any],
    *,
    table_name: str,
    values: dict[str, object],
    row_code: str,
) -> dict[str, object]:
    columns = connection.execute(
        """
        SELECT column_name, is_nullable, column_default, is_generated
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    ).fetchall()
    assert columns, f"PostgreSQL head 缺少关系表 {table_name}"
    result = dict(values)
    defaults: dict[str, object] = {
        "id": uuid4(),
        "source_key": f"cp2-pg-row:{row_code}",
        "valid_from": NOW,
        "effective_from": NOW,
        "published_at": NOW,
        "status": "confirmed",
        "version": 1,
    }
    uncovered: list[str] = []
    for column_name, nullable, column_default, generated in columns:
        if column_name in result or nullable == "YES":
            continue
        if column_default is not None or generated != "NEVER":
            continue
        if column_name in defaults:
            result[column_name] = defaults[column_name]
        else:
            uncovered.append(column_name)
    if uncovered:
        pytest.fail(f"PG 行为门无法构造 {table_name} 的有效基线行；尚未提供的必填列为 {uncovered}")
    return result


def _insert_dynamic_row(
    connection: psycopg.Connection[Any],
    *,
    table_name: str,
    values: dict[str, object],
) -> None:
    columns = tuple(values)
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )
    connection.execute(statement, tuple(values[column] for column in columns))


def _campus_target_case(
    connection: psycopg.Connection[Any],
    *,
    target: str,
    ids: dict[str, UUID],
) -> _CampusTargetCase:
    shapes = _foreign_key_shapes(connection)
    if target == "organization_relation":
        shape = _organization_relation_shape(connection, shapes)
        return _CampusTargetCase(
            code=target,
            table_name=shape.table_name,
            target_column=_single_column(shape, "campus_locations"),
            base_values={
                "school_id": ids["school_id"],
                _single_column(shape, "organization_units"): ids["organization_id"],
                "source_id": ids["source_id"],
            },
        )
    if target in {"student_primary", "student_administrative"}:
        shape = _student_relationship_shape(shapes)
        campus_columns = shape.columns_by_target["campus_locations"]
        primary_column = next(name for name in campus_columns if "primary" in name)
        administrative_column = next(name for name in campus_columns if "administrative" in name)
        target_column = primary_column if target == "student_primary" else administrative_column
        other_column = administrative_column if target == "student_primary" else primary_column
        return _CampusTargetCase(
            code=target,
            table_name=shape.table_name,
            target_column=target_column,
            base_values={
                "school_id": ids["school_id"],
                "student_id": ids["student_id"],
                "student_number": "CP2-PG-1",
                "enrollment_status": "enrolled",
                "residence_mode": "day",
                other_column: ids["campus_id"],
                "source_id": ids["source_id"],
            },
        )
    shape = _policy_scope_shape(connection, shapes)
    policy_target = _policy_target_table(shape)
    policy_id = (
        ids["policy_version_id"] if policy_target == "school_policy_versions" else ids["policy_id"]
    )
    return _CampusTargetCase(
        code=target,
        table_name=shape.table_name,
        target_column=_single_column(shape, "campus_locations"),
        base_values={
            "school_id": ids["school_id"],
            _single_column(shape, policy_target): policy_id,
            _single_column(shape, "organization_units"): None,
            "source_id": ids["source_id"],
        },
    )


def _second_root_values(
    case: _CampusTargetCase,
    connection: psycopg.Connection[Any],
    ids: dict[str, UUID],
) -> dict[str, object]:
    values = dict(case.base_values)
    shapes = _foreign_key_shapes(connection)
    shape = next(item for item in shapes if item.table_name == case.table_name)
    if case.code == "organization_relation":
        values[_single_column(shape, "organization_units")] = ids["second_organization_id"]
    elif case.code in {"student_primary", "student_administrative"}:
        values["student_id"] = ids["second_student_id"]
        values["student_number"] = "CP2-PG-2"
    else:
        target = _policy_target_table(shape)
        values[_single_column(shape, target)] = (
            ids["second_policy_version_id"]
            if target == "school_policy_versions"
            else ids["second_policy_id"]
        )
    return values


@pytest.mark.integration
@pytest.mark.parametrize(
    "target",
    ("organization_relation", "student_primary", "student_administrative", "policy_scope"),
)
def test_campus_targets_reject_non_campus_location_in_postgresql(
    target: str,
    current_master_data_schema: PostgresRoles,
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    del current_master_data_schema
    connection = current_owner_connection
    ids = _insert_school_fact_parents(connection, code=target)
    case = _campus_target_case(connection, target=target, ids=ids)

    valid_values = _required_insert_values(
        connection,
        table_name=case.table_name,
        values={**case.base_values, case.target_column: ids["campus_id"]},
        row_code=f"{target}:valid",
    )
    _insert_dynamic_row(
        connection,
        table_name=case.table_name,
        values=valid_values,
    )

    invalid_values = _required_insert_values(
        connection,
        table_name=case.table_name,
        values={
            **_second_root_values(case, connection, ids),
            case.target_column: ids["building_id"],
        },
        row_code=f"{target}:invalid",
    )
    with pytest.raises(psycopg.IntegrityError), connection.transaction():
        _insert_dynamic_row(
            connection,
            table_name=case.table_name,
            values=invalid_values,
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.integration
@pytest.mark.parametrize(
    "target",
    ("organization_relation", "student_primary", "student_administrative", "policy_scope"),
)
def test_campus_target_interval_requires_campus_kind_for_its_full_duration(
    target: str,
    isolated_migration_schema: PostgresRoles,
    isolated_owner_connection: psycopg.Connection[Any],
) -> None:
    """A relation cannot start as campus-scoped and cross a known kind demotion."""

    del isolated_migration_schema
    connection = isolated_owner_connection
    ids = _insert_school_fact_parents(
        connection,
        code=f"campus-interval-{target}",
        include_location_slices=False,
    )
    second_campus_id = uuid4()
    demotion_at = NOW + timedelta(days=1)
    connection.execute(
        "INSERT INTO campus_locations "
        "(id, school_id, source_key, parent_location_id, kind, status, version) "
        "VALUES (%s, %s, %s, NULL, 'campus', 'active', 1)",
        (
            second_campus_id,
            ids["school_id"],
            f"cp3-campus-interval-parent:{target}",
        ),
    )
    connection.execute(
        "INSERT INTO campus_location_versions "
        "(id, school_id, location_id, parent_location_id, kind, business_code, "
        "display_name, aliases, display_path, valid_from, valid_to, source_kind, "
        "source_ref, status, version) VALUES "
        "(%s, %s, %s, NULL, 'campus', 'TARGET', '目标校区', ARRAY[]::varchar[], "
        "'/目标校区', %s, %s, 'manual', %s, 'confirmed', 1), "
        "(%s, %s, %s, %s, 'building', 'TARGET', '已转为楼栋', ARRAY[]::varchar[], "
        "'/上级校区/已转为楼栋', %s, NULL, 'manual', %s, 'confirmed', 2), "
        "(%s, %s, %s, NULL, 'campus', 'PARENT', '上级校区', ARRAY[]::varchar[], "
        "'/上级校区', %s, NULL, 'manual', %s, 'confirmed', 1)",
        (
            uuid4(),
            ids["school_id"],
            ids["campus_id"],
            NOW,
            demotion_at,
            f"campus-interval:{target}:before",
            uuid4(),
            ids["school_id"],
            ids["campus_id"],
            second_campus_id,
            demotion_at,
            f"campus-interval:{target}:after",
            uuid4(),
            ids["school_id"],
            second_campus_id,
            NOW,
            f"campus-interval:{target}:parent",
        ),
    )
    case = _campus_target_case(connection, target=target, ids=ids)
    values = _required_insert_values(
        connection,
        table_name=case.table_name,
        values={
            **case.base_values,
            case.target_column: ids["campus_id"],
            "valid_from": NOW + timedelta(hours=1),
            "valid_to": None,
        },
        row_code=f"campus-interval:{target}",
    )
    with pytest.raises(psycopg.IntegrityError) as exc_info, connection.transaction():
        _insert_dynamic_row(connection, table_name=case.table_name, values=values)
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert exc_info.value.sqlstate == "23514"
    assert exc_info.value.diag.constraint_name == "ck_school_fact_campus_target_kind"


@pytest.mark.integration
def test_referenced_campus_cannot_later_be_changed_into_a_non_campus_node(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """The reverse guard prevents a valid campus FK from drifting after insertion."""

    with _residence_harness(isolated_migration_schema) as harness:
        harness.confirm(
            _plan(
                label="add-second-campus-for-reverse-target-guard",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": "location",
                        "source_key": "location:second-campus",
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {"sheet_code": "locations", "row_number": 2},
                        "content": {
                            "kind": "campus",
                            "business_code": "SECOND",
                            "display_name": "第二校区",
                            "display_path": "/第二校区",
                            "status": "active",
                        },
                    },
                ),
            ),
            label="add-second-campus-for-reverse-target-guard",
        )
        with harness.factory() as session:  # type: ignore[operator]
            campus_id, second_campus_id = session.execute(
                text(
                    "SELECT "
                    "(SELECT id FROM campus_locations WHERE school_id = :school_id "
                    " AND source_key = 'location:campus'), "
                    "(SELECT id FROM campus_locations WHERE school_id = :school_id "
                    " AND source_key = 'location:second-campus')"
                ),
                {"school_id": harness.school_id},
            ).one()
            transaction = session.begin_nested()
            try:
                with pytest.raises(DBAPIError) as exc_info:
                    session.execute(
                        text(
                            "UPDATE campus_location_versions SET valid_to = :cutover "
                            "WHERE school_id = :school_id AND location_id = :campus_id "
                            "AND version = 1"
                        ),
                        {
                            "school_id": harness.school_id,
                            "campus_id": campus_id,
                            "cutover": NOW + timedelta(days=1),
                        },
                    )
                    session.execute(
                        text(
                            "INSERT INTO campus_location_versions "
                            "(id, school_id, location_id, parent_location_id, kind, "
                            "business_code, display_name, aliases, display_path, valid_from, "
                            "source_kind, source_ref, status, version) "
                            "SELECT :version_id, school_id, location_id, :parent_id, "
                            "'dorm_building', business_code, display_name, aliases, "
                            "display_path, :cutover, source_kind, :source_ref, status, 2 "
                            "FROM campus_location_versions WHERE school_id = :school_id "
                            "AND location_id = :campus_id AND version = 1"
                        ),
                        {
                            "version_id": uuid4(),
                            "school_id": harness.school_id,
                            "campus_id": campus_id,
                            "parent_id": second_campus_id,
                            "cutover": NOW + timedelta(days=1),
                            "source_ref": f"reverse-campus-demotion:{uuid4().hex}",
                        },
                    )
                original = exc_info.value.orig
                assert getattr(original, "sqlstate", None) == "23514"
                assert (
                    getattr(getattr(original, "diag", None), "constraint_name", None)
                    == "ck_school_fact_referenced_campus_target_kind"
                )
            finally:
                transaction.rollback()

        with harness.factory() as session:  # type: ignore[operator]
            shape = session.execute(
                text("SELECT kind, parent_location_id FROM campus_locations WHERE id = :id"),
                {"id": campus_id},
            ).one()
        assert shape == ("campus", None)


@pytest.mark.integration
@pytest.mark.parametrize("first_write", ("relation", "campus_kind"))
def test_campus_target_and_kind_change_serialize_across_connections(
    isolated_migration_schema: PostgresRoles,
    first_write: str,
) -> None:
    """Forward and reverse campus-kind checks cannot write-skew around each other."""

    roles = isolated_migration_schema
    with psycopg.connect(
        **roles.migration_connection_parameters,
        autocommit=False,
    ) as seed:
        seed.execute("SET ROLE campus_ai_db_owner")
        ids = _insert_school_fact_parents(
            seed,
            code=f"target-race-{first_write}",
            include_location_slices=False,
        )
        second_campus_id = uuid4()
        second_campus_source_key = f"cp3-target-race-second-campus:{first_write}"
        seed.execute(
            "INSERT INTO campus_locations "
            "(id, school_id, source_key, parent_location_id, kind, status, version) "
            "VALUES (%s, %s, %s, NULL, 'campus', 'active', 1)",
            (
                second_campus_id,
                ids["school_id"],
                second_campus_source_key,
            ),
        )
        _insert_confirmed_location_slices(
            seed,
            school_id=ids["school_id"],
            slices=(
                _LocationSliceSeed(
                    location_id=ids["campus_id"],
                    source_key=f"cp2-pg-campus:target-race-{first_write}",
                    parent_location_id=None,
                    kind="campus",
                    business_code="TARGET",
                    display_name="目标校区",
                    aliases=(),
                    display_path="/目标校区",
                    effective_at=NOW,
                ),
                _LocationSliceSeed(
                    location_id=ids["building_id"],
                    source_key=f"cp2-pg-building:target-race-{first_write}",
                    parent_location_id=ids["campus_id"],
                    kind="building",
                    business_code="BUILDING",
                    display_name="楼栋",
                    aliases=(),
                    display_path="/目标校区/楼栋",
                    effective_at=NOW,
                ),
                _LocationSliceSeed(
                    location_id=second_campus_id,
                    source_key=second_campus_source_key,
                    parent_location_id=None,
                    kind="campus",
                    business_code="PARENT",
                    display_name="上级校区",
                    aliases=(),
                    display_path="/上级校区",
                    effective_at=NOW,
                ),
            ),
        )
        case = _campus_target_case(seed, target="organization_relation", ids=ids)
        relation_values = _required_insert_values(
            seed,
            table_name=case.table_name,
            values={**case.base_values, case.target_column: ids["campus_id"]},
            row_code=f"target-race:{first_write}",
        )
        seed.commit()

    def insert_relation(connection: psycopg.Connection[Any]) -> None:
        _insert_dynamic_row(
            connection,
            table_name=case.table_name,
            values=relation_values,
        )

    def change_campus_kind(connection: psycopg.Connection[Any]) -> None:
        cutover = NOW + timedelta(days=1)
        connection.execute(
            "UPDATE campus_location_versions SET valid_to = %s "
            "WHERE school_id = %s AND location_id = %s AND version = 1",
            (cutover, ids["school_id"], ids["campus_id"]),
        )
        _insert_confirmed_location_slices(
            connection,
            school_id=ids["school_id"],
            slices=(
                _LocationSliceSeed(
                    location_id=ids["campus_id"],
                    source_key=f"cp2-pg-campus:target-race-{first_write}",
                    parent_location_id=second_campus_id,
                    kind="dorm_building",
                    business_code="TARGET",
                    display_name="目标校区",
                    aliases=(),
                    display_path="/上级校区/目标校区",
                    effective_at=cutover,
                    target_version=2,
                    expected_version=1,
                ),
            ),
        )
        connection.execute(
            "UPDATE campus_locations "
            "SET parent_location_id = %s, kind = 'dorm_building', version = 2 "
            "WHERE school_id = %s AND id = %s",
            (
                second_campus_id,
                ids["school_id"],
                ids["campus_id"],
            ),
        )

    first_action, second_action = (
        (insert_relation, change_campus_kind)
        if first_write == "relation"
        else (change_campus_kind, insert_relation)
    )
    outcomes, serialization_wait = _run_competing_owner_transactions(
        roles,
        first_action=first_action,
        second_action=second_action,
        label=f"campus-target-{first_write}",
    )

    assert serialization_wait == ("Lock", "advisory")
    assert sorted(outcomes) == ["23514", "committed"]
    with psycopg.connect(
        **roles.migration_connection_parameters,
        autocommit=True,
    ) as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        final_row = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM campus_location_versions "
            " WHERE school_id = %s AND location_id = %s AND kind <> 'campus'), "
            "(SELECT count(*) FROM organization_campus_relationships "
            " WHERE school_id = %s AND campus_location_id = %s)",
            (
                ids["school_id"],
                ids["campus_id"],
                ids["school_id"],
                ids["campus_id"],
            ),
        ).fetchone()
    assert final_row is not None
    demoted_slice_count, relation_count = final_row
    assert not (demoted_slice_count > 0 and relation_count > 0)


@pytest.mark.integration
def test_organization_tree_rejects_self_parent(
    isolated_migration_schema: PostgresRoles,
    isolated_owner_connection: psycopg.Connection[Any],
) -> None:
    """An organization identity cannot be its own parent."""

    del isolated_migration_schema
    connection = isolated_owner_connection
    ids = _insert_school_fact_parents(connection, code="organization-self-cycle")
    with pytest.raises(psycopg.IntegrityError) as exc_info, connection.transaction():
        connection.execute(
            "UPDATE organization_units SET parent_organization_unit_id = %s "
            "WHERE school_id = %s AND id = %s",
            (ids["organization_id"], ids["school_id"], ids["organization_id"]),
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert exc_info.value.sqlstate == "23514"
    assert exc_info.value.diag.constraint_name == "ck_organization_units_acyclic"


@pytest.mark.integration
def test_organization_tree_rejects_direct_two_node_cycle(
    isolated_migration_schema: PostgresRoles,
    isolated_owner_connection: psycopg.Connection[Any],
) -> None:
    """A sequential two-node parent cycle cannot commit."""

    del isolated_migration_schema
    connection = isolated_owner_connection
    ids = _insert_school_fact_parents(connection, code="organization-direct-cycle")
    connection.execute(
        "UPDATE organization_units SET parent_organization_unit_id = %s "
        "WHERE school_id = %s AND id = %s",
        (ids["second_organization_id"], ids["school_id"], ids["organization_id"]),
    )
    with pytest.raises(psycopg.IntegrityError) as exc_info, connection.transaction():
        connection.execute(
            "UPDATE organization_units SET parent_organization_unit_id = %s "
            "WHERE school_id = %s AND id = %s",
            (ids["organization_id"], ids["school_id"], ids["second_organization_id"]),
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert exc_info.value.sqlstate == "23514"
    assert exc_info.value.diag.constraint_name == "ck_organization_units_acyclic"


@pytest.mark.integration
def test_organization_tree_rejects_indirect_cycle(
    isolated_migration_schema: PostgresRoles,
    isolated_owner_connection: psycopg.Connection[Any],
) -> None:
    """A cycle through more than one ancestor is rejected without recursive looping."""

    del isolated_migration_schema
    connection = isolated_owner_connection
    ids = _insert_school_fact_parents(connection, code="organization-indirect-cycle")
    third_id = uuid4()
    connection.execute(
        "INSERT INTO organization_units "
        "(id, school_id, source_key, parent_organization_unit_id, kind, status, version) "
        "VALUES (%s, %s, %s, %s, 'department', 'active', 1)",
        (
            third_id,
            ids["school_id"],
            f"cp2-pg-organization-third:{uuid4().hex}",
            ids["second_organization_id"],
        ),
    )
    connection.execute(
        "UPDATE organization_units SET parent_organization_unit_id = %s "
        "WHERE school_id = %s AND id = %s",
        (ids["organization_id"], ids["school_id"], ids["second_organization_id"]),
    )
    with pytest.raises(psycopg.IntegrityError) as exc_info, connection.transaction():
        connection.execute(
            "UPDATE organization_units SET parent_organization_unit_id = %s "
            "WHERE school_id = %s AND id = %s",
            (third_id, ids["school_id"], ids["organization_id"]),
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert exc_info.value.sqlstate == "23514"
    assert exc_info.value.diag.constraint_name == "ck_organization_units_acyclic"


@pytest.mark.integration
def test_organization_version_slices_reject_temporal_cycle(
    isolated_migration_schema: PostgresRoles,
    isolated_owner_connection: psycopg.Connection[Any],
) -> None:
    """Parent slices may never form a cycle during an overlapping effective interval."""

    del isolated_migration_schema
    connection = isolated_owner_connection
    ids = _insert_school_fact_parents(connection, code="organization-temporal-cycle")
    cutover = NOW + timedelta(days=1)
    restore_at = cutover + timedelta(days=1)
    _insert_confirmed_organization_slices(
        connection,
        school_id=ids["school_id"],
        slices=tuple(
            _OrganizationSliceSeed(
                organization_id=organization_id,
                source_key=f"cp2-pg-{key}:organization-temporal-cycle",
                parent_organization_id=None,
                business_code=f"ORG-{ordinal}",
                display_name=f"组织{ordinal}",
                display_path=f"/组织{ordinal}",
                effective_at=NOW,
            )
            for ordinal, (key, organization_id) in enumerate(
                (
                    ("organization_id", ids["organization_id"]),
                    ("second_organization_id", ids["second_organization_id"]),
                ),
                start=1,
            )
        ),
    )
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    connection.execute("SET CONSTRAINTS ALL DEFERRED")

    connection.execute(
        "UPDATE organization_unit_versions SET valid_to = %s "
        "WHERE school_id = %s AND organization_unit_id = %s AND version = 1",
        (cutover, ids["school_id"], ids["second_organization_id"]),
    )
    _insert_confirmed_organization_slices(
        connection,
        school_id=ids["school_id"],
        slices=(
            _OrganizationSliceSeed(
                organization_id=ids["second_organization_id"],
                source_key=("cp2-pg-second_organization_id:organization-temporal-cycle"),
                parent_organization_id=ids["organization_id"],
                business_code="ORG-2",
                display_name="组织2",
                display_path="/组织1/组织2",
                effective_at=cutover,
                target_version=2,
                expected_version=1,
            ),
        ),
    )
    connection.execute(
        "UPDATE organization_units SET parent_organization_unit_id = %s, version = 2 "
        "WHERE school_id = %s AND id = %s",
        (ids["organization_id"], ids["school_id"], ids["second_organization_id"]),
    )
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    connection.execute("SET CONSTRAINTS ALL DEFERRED")

    connection.execute(
        "UPDATE organization_unit_versions SET valid_to = %s "
        "WHERE school_id = %s AND organization_unit_id = %s AND version = 2",
        (restore_at, ids["school_id"], ids["second_organization_id"]),
    )
    _insert_confirmed_organization_slices(
        connection,
        school_id=ids["school_id"],
        slices=(
            _OrganizationSliceSeed(
                organization_id=ids["second_organization_id"],
                source_key=("cp2-pg-second_organization_id:organization-temporal-cycle"),
                parent_organization_id=None,
                business_code="ORG-2",
                display_name="组织2",
                display_path="/组织2",
                effective_at=restore_at,
                target_version=3,
                expected_version=2,
            ),
        ),
    )
    connection.execute(
        "UPDATE organization_units SET parent_organization_unit_id = NULL, version = 3 "
        "WHERE school_id = %s AND id = %s",
        (ids["school_id"], ids["second_organization_id"]),
    )
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    connection.execute("SET CONSTRAINTS ALL DEFERRED")

    with pytest.raises(psycopg.IntegrityError) as exc_info, connection.transaction():
        connection.execute(
            "UPDATE organization_unit_versions SET valid_to = %s "
            "WHERE school_id = %s AND organization_unit_id = %s AND version = 1",
            (cutover, ids["school_id"], ids["organization_id"]),
        )
        _insert_confirmed_organization_slices(
            connection,
            school_id=ids["school_id"],
            slices=(
                _OrganizationSliceSeed(
                    organization_id=ids["organization_id"],
                    source_key="cp2-pg-organization_id:organization-temporal-cycle",
                    parent_organization_id=ids["second_organization_id"],
                    business_code="ORG-1",
                    display_name="组织1",
                    display_path="/组织2/组织1",
                    effective_at=cutover,
                    target_version=2,
                    expected_version=1,
                ),
            ),
        )
        connection.execute(
            "UPDATE organization_units SET parent_organization_unit_id = %s, version = 2 "
            "WHERE school_id = %s AND id = %s",
            (ids["second_organization_id"], ids["school_id"], ids["organization_id"]),
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert exc_info.value.sqlstate == "23514"
    assert exc_info.value.diag.constraint_name == "ck_organization_unit_versions_acyclic"


@pytest.mark.integration
def test_location_version_slices_reject_temporal_cycle(
    isolated_migration_schema: PostgresRoles,
    isolated_owner_connection: psycopg.Connection[Any],
) -> None:
    """Location parent slices cannot form an overlapping historical cycle."""

    del isolated_migration_schema
    connection = isolated_owner_connection
    ids = _insert_school_fact_parents(
        connection,
        code="location-temporal-cycle",
        include_location_slices=False,
    )
    second_room_id = uuid4()
    second_room_source_key = f"cp2-pg-second-room:{uuid4().hex}"
    cutover = NOW + timedelta(days=1)
    connection.execute(
        "INSERT INTO campus_locations "
        "(id, school_id, source_key, parent_location_id, kind, status, version) "
        "VALUES (%s, %s, %s, %s, 'building', 'active', 1)",
        (
            second_room_id,
            ids["school_id"],
            second_room_source_key,
            ids["campus_id"],
        ),
    )
    _insert_confirmed_location_slices(
        connection,
        school_id=ids["school_id"],
        slices=(
            _LocationSliceSeed(
                location_id=ids["campus_id"],
                source_key="cp2-pg-campus:location-temporal-cycle",
                parent_location_id=None,
                kind="campus",
                business_code="CAMPUS",
                display_name="校区",
                aliases=(),
                display_path="/校区",
                effective_at=NOW,
            ),
            _LocationSliceSeed(
                location_id=ids["building_id"],
                source_key="cp2-pg-building:location-temporal-cycle",
                parent_location_id=ids["campus_id"],
                kind="building",
                business_code="ROOM-1",
                display_name="房间1",
                aliases=(),
                display_path="/校区/房间1",
                effective_at=NOW,
            ),
            _LocationSliceSeed(
                location_id=second_room_id,
                source_key=second_room_source_key,
                parent_location_id=ids["campus_id"],
                kind="building",
                business_code="ROOM-2",
                display_name="房间2",
                aliases=(),
                display_path="/校区/房间2",
                effective_at=NOW,
            ),
        ),
    )
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    connection.execute("SET CONSTRAINTS ALL DEFERRED")

    connection.execute(
        "UPDATE campus_location_versions SET valid_to = %s "
        "WHERE school_id = %s AND location_id = %s AND version = 1",
        (cutover, ids["school_id"], ids["building_id"]),
    )
    _insert_confirmed_location_slices(
        connection,
        school_id=ids["school_id"],
        slices=(
            _LocationSliceSeed(
                location_id=ids["building_id"],
                source_key="cp2-pg-building:location-temporal-cycle",
                parent_location_id=second_room_id,
                kind="building",
                business_code="ROOM-1",
                display_name="房间1",
                aliases=(),
                display_path="/房间2/房间1",
                effective_at=cutover,
                target_version=2,
                expected_version=1,
            ),
        ),
    )
    connection.execute(
        "UPDATE campus_locations SET parent_location_id = %s, version = 2 "
        "WHERE school_id = %s AND id = %s",
        (second_room_id, ids["school_id"], ids["building_id"]),
    )
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    connection.execute("SET CONSTRAINTS ALL DEFERRED")

    with pytest.raises(psycopg.IntegrityError) as exc_info, connection.transaction():
        connection.execute(
            "UPDATE campus_location_versions SET valid_to = %s "
            "WHERE school_id = %s AND location_id = %s AND version = 1",
            (cutover, ids["school_id"], second_room_id),
        )
        _insert_confirmed_location_slices(
            connection,
            school_id=ids["school_id"],
            slices=(
                _LocationSliceSeed(
                    location_id=second_room_id,
                    source_key=second_room_source_key,
                    parent_location_id=ids["building_id"],
                    kind="building",
                    business_code="ROOM-2",
                    display_name="房间2",
                    aliases=(),
                    display_path="/房间1/房间2",
                    effective_at=cutover,
                    target_version=2,
                    expected_version=1,
                ),
            ),
        )
        connection.execute(
            "UPDATE campus_locations SET parent_location_id = %s, version = 2 "
            "WHERE school_id = %s AND id = %s",
            (ids["building_id"], ids["school_id"], second_room_id),
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert exc_info.value.sqlstate == "23514"
    assert exc_info.value.diag.constraint_name == "ck_campus_location_versions_acyclic"


@pytest.mark.integration
@pytest.mark.parametrize("fact_kind", ("location", "organization"))
def test_child_structure_slice_cannot_start_before_its_parent_slice(
    isolated_migration_schema: PostgresRoles,
    isolated_owner_connection: psycopg.Connection[Any],
    fact_kind: str,
) -> None:
    """A stable parent identity is not enough before its own structure slice begins."""

    del isolated_migration_schema
    connection = isolated_owner_connection
    ids = _insert_school_fact_parents(
        connection,
        code=f"future-{fact_kind}-parent",
        include_location_slices=fact_kind != "location",
        include_initial_organization_parent=fact_kind == "organization",
    )
    future = NOW + timedelta(days=1)
    expected_constraint = (
        "ck_campus_location_versions_parent_coverage"
        if fact_kind == "location"
        else "ck_organization_unit_versions_parent_coverage"
    )
    with pytest.raises(psycopg.IntegrityError) as exc_info, connection.transaction():
        if fact_kind == "location":
            _insert_confirmed_location_slices(
                connection,
                school_id=ids["school_id"],
                slices=(
                    _LocationSliceSeed(
                        location_id=ids["campus_id"],
                        source_key="cp2-pg-campus:future-location-parent",
                        parent_location_id=None,
                        kind="campus",
                        business_code="CAMPUS",
                        display_name="未来校区",
                        aliases=(),
                        display_path="/未来校区",
                        effective_at=future,
                    ),
                    _LocationSliceSeed(
                        location_id=ids["building_id"],
                        source_key="cp2-pg-building:future-location-parent",
                        parent_location_id=ids["campus_id"],
                        kind="building",
                        business_code="BUILDING",
                        display_name="过早楼栋",
                        aliases=(),
                        display_path="/未来校区/过早楼栋",
                        effective_at=NOW,
                    ),
                ),
            )
        else:
            _insert_confirmed_organization_slices(
                connection,
                school_id=ids["school_id"],
                slices=(
                    _OrganizationSliceSeed(
                        organization_id=ids["organization_id"],
                        source_key=("cp2-pg-organization_id:future-organization-parent"),
                        parent_organization_id=None,
                        business_code="PARENT",
                        display_name="未来上级",
                        display_path="/未来上级",
                        effective_at=future,
                    ),
                    _OrganizationSliceSeed(
                        organization_id=ids["second_organization_id"],
                        source_key=("cp2-pg-second_organization_id:future-organization-parent"),
                        parent_organization_id=ids["organization_id"],
                        business_code="CHILD",
                        display_name="过早子组织",
                        display_path="/未来上级/过早子组织",
                        effective_at=NOW,
                    ),
                ),
            )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert exc_info.value.sqlstate == "23514"
    assert exc_info.value.diag.constraint_name == expected_constraint


@pytest.mark.integration
@pytest.mark.parametrize("fact_kind", ("location", "organization"))
def test_parent_slice_cannot_close_while_child_structure_remains_open(
    isolated_migration_schema: PostgresRoles,
    isolated_owner_connection: psycopg.Connection[Any],
    fact_kind: str,
) -> None:
    """Reverse updates cannot leave an already stored child slice without its parent."""

    del isolated_migration_schema
    connection = isolated_owner_connection
    ids = _insert_school_fact_parents(
        connection,
        code=f"closed-{fact_kind}-parent",
        include_location_slices=fact_kind != "location",
        include_initial_organization_parent=fact_kind == "organization",
    )
    expected_constraint = (
        "ck_campus_location_versions_parent_coverage"
        if fact_kind == "location"
        else "ck_organization_unit_versions_parent_coverage"
    )
    if fact_kind == "location":
        _insert_confirmed_location_slices(
            connection,
            school_id=ids["school_id"],
            slices=(
                _LocationSliceSeed(
                    location_id=ids["campus_id"],
                    source_key="cp2-pg-campus:closed-location-parent",
                    parent_location_id=None,
                    kind="campus",
                    business_code="CAMPUS",
                    display_name="校区",
                    aliases=(),
                    display_path="/校区",
                    effective_at=NOW,
                ),
                _LocationSliceSeed(
                    location_id=ids["building_id"],
                    source_key="cp2-pg-building:closed-location-parent",
                    parent_location_id=ids["campus_id"],
                    kind="building",
                    business_code="BUILDING",
                    display_name="楼栋",
                    aliases=(),
                    display_path="/校区/楼栋",
                    effective_at=NOW,
                ),
            ),
        )
        table_name, id_column, parent_id, lineage_trigger = (
            "campus_location_versions",
            "location_id",
            ids["campus_id"],
            "trg_sf18_require_location_version_structure_lineage",
        )
    else:
        _insert_confirmed_organization_slices(
            connection,
            school_id=ids["school_id"],
            slices=(
                _OrganizationSliceSeed(
                    organization_id=ids["organization_id"],
                    source_key="cp2-pg-organization_id:closed-organization-parent",
                    parent_organization_id=None,
                    business_code="PARENT",
                    display_name="上级",
                    display_path="/上级",
                    effective_at=NOW,
                ),
                _OrganizationSliceSeed(
                    organization_id=ids["second_organization_id"],
                    source_key=("cp2-pg-second_organization_id:closed-organization-parent"),
                    parent_organization_id=ids["organization_id"],
                    business_code="CHILD",
                    display_name="子组织",
                    display_path="/上级/子组织",
                    effective_at=NOW,
                ),
            ),
        )
        table_name, id_column, parent_id, lineage_trigger = (
            "organization_unit_versions",
            "organization_unit_id",
            ids["organization_id"],
            "trg_sf18_require_organization_version_structure_lineage",
        )
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    connection.commit()

    with connection.transaction():
        with pytest.raises(psycopg.IntegrityError) as exc_info, connection.transaction():
            # This negative test isolates the parent-coverage constraint only. The
            # owner-scoped trigger change and invalid close both live in this savepoint;
            # its rollback restores the formal-lineage gate before any other path runs.
            connection.execute(
                sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(
                    sql.Identifier(table_name),
                    sql.Identifier(lineage_trigger),
                )
            )
            connection.execute(
                sql.SQL(
                    "UPDATE {} SET valid_to = %s WHERE school_id = %s AND {} = %s AND version = 1"
                ).format(sql.Identifier(table_name), sql.Identifier(id_column)),
                (NOW + timedelta(days=1), ids["school_id"], parent_id),
            )
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        trigger_state = connection.execute(
            "SELECT tgenabled FROM pg_catalog.pg_trigger "
            "WHERE tgrelid = pg_catalog.to_regclass(%s) AND tgname = %s",
            (table_name, lineage_trigger),
        ).fetchone()
        assert trigger_state == ("O",)
        parent_valid_to = connection.execute(
            sql.SQL(
                "SELECT valid_to FROM {} WHERE school_id = %s AND {} = %s AND version = 1"
            ).format(sql.Identifier(table_name), sql.Identifier(id_column)),
            (ids["school_id"], parent_id),
        ).fetchone()
        assert parent_valid_to == (None,)
    assert exc_info.value.sqlstate == "23514"
    assert exc_info.value.diag.constraint_name == expected_constraint


@pytest.mark.integration
@pytest.mark.parametrize("fact_kind", ("location", "organization"))
def test_direct_stable_structure_update_requires_formal_lineage(
    isolated_migration_schema: PostgresRoles,
    isolated_owner_connection: psycopg.Connection[Any],
    fact_kind: str,
) -> None:
    """Stable current projections cannot be changed without their version and lineage."""

    del isolated_migration_schema
    connection = isolated_owner_connection
    ids = _insert_school_fact_parents(connection, code=f"untracked-{fact_kind}-structure")
    connection.commit()
    expected_constraint = (
        "ck_campus_location_structure_lineage"
        if fact_kind == "location"
        else "ck_organization_unit_structure_lineage"
    )
    with pytest.raises(psycopg.IntegrityError) as exc_info, connection.transaction():
        if fact_kind == "location":
            connection.execute(
                "UPDATE campus_locations SET kind = 'dorm_building', version = 2 "
                "WHERE school_id = %s AND id = %s",
                (ids["school_id"], ids["building_id"]),
            )
        else:
            connection.execute(
                "UPDATE organization_units SET parent_organization_unit_id = %s, version = 2 "
                "WHERE school_id = %s AND id = %s",
                (
                    ids["organization_id"],
                    ids["school_id"],
                    ids["second_organization_id"],
                ),
            )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert exc_info.value.sqlstate == "23514"
    assert exc_info.value.diag.constraint_name == expected_constraint


@pytest.mark.integration
def test_concurrent_organization_reparents_cannot_write_skew_a_cycle(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Two connections serialize the parent invariant and at most one can commit."""

    roles = isolated_migration_schema
    with psycopg.connect(
        **roles.migration_connection_parameters,
        autocommit=False,
    ) as seed:
        seed.execute("SET ROLE campus_ai_db_owner")
        ids = _insert_school_fact_parents(seed, code="organization-concurrent-cycle")
        _insert_confirmed_organization_slices(
            seed,
            school_id=ids["school_id"],
            slices=tuple(
                _OrganizationSliceSeed(
                    organization_id=organization_id,
                    source_key=f"cp2-pg-{key}:organization-concurrent-cycle",
                    parent_organization_id=None,
                    business_code=f"CONCURRENT-{ordinal}",
                    display_name=f"并发组织{ordinal}",
                    display_path=f"/并发组织{ordinal}",
                    effective_at=NOW,
                )
                for ordinal, (key, organization_id) in enumerate(
                    (
                        ("organization_id", ids["organization_id"]),
                        ("second_organization_id", ids["second_organization_id"]),
                    ),
                    start=1,
                )
            ),
        )
        seed.commit()

    cutover = NOW + timedelta(days=1)

    def reparent_version(
        connection: psycopg.Connection[Any],
        *,
        organization_id: UUID,
        parent_id: UUID,
        ordinal: int,
    ) -> None:
        source_key = (
            "cp2-pg-organization_id:organization-concurrent-cycle"
            if ordinal == 1
            else "cp2-pg-second_organization_id:organization-concurrent-cycle"
        )
        connection.execute(
            "UPDATE organization_unit_versions SET valid_to = %s "
            "WHERE school_id = %s AND organization_unit_id = %s AND version = 1",
            (cutover, ids["school_id"], organization_id),
        )
        _insert_confirmed_organization_slices(
            connection,
            school_id=ids["school_id"],
            slices=(
                _OrganizationSliceSeed(
                    organization_id=organization_id,
                    source_key=source_key,
                    parent_organization_id=parent_id,
                    business_code=f"CONCURRENT-{ordinal}",
                    display_name=f"并发组织{ordinal}",
                    display_path=f"/并发组织{3 - ordinal}/并发组织{ordinal}",
                    effective_at=cutover,
                    target_version=2,
                    expected_version=1,
                ),
            ),
        )
        connection.execute(
            "UPDATE organization_units "
            "SET parent_organization_unit_id = %s, version = 2 "
            "WHERE school_id = %s AND id = %s",
            (
                parent_id,
                ids["school_id"],
                organization_id,
            ),
        )

    def first_reparent(connection: psycopg.Connection[Any]) -> None:
        reparent_version(
            connection,
            organization_id=ids["organization_id"],
            parent_id=ids["second_organization_id"],
            ordinal=1,
        )

    def second_reparent(connection: psycopg.Connection[Any]) -> None:
        reparent_version(
            connection,
            organization_id=ids["second_organization_id"],
            parent_id=ids["organization_id"],
            ordinal=2,
        )

    outcomes, serialization_wait = _run_competing_owner_transactions(
        roles,
        first_action=first_reparent,
        second_action=second_reparent,
        label="organization-cycle",
    )

    assert serialization_wait == ("Lock", "advisory")
    assert sorted(outcomes) == ["23514", "committed"]


@pytest.mark.integration
@pytest.mark.parametrize("violation", ("cross_school", "empty_interval"))
def test_organization_campus_relationship_enforces_school_and_interval(
    violation: str,
    current_master_data_schema: PostgresRoles,
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    del current_master_data_schema
    connection = current_owner_connection
    first = _insert_school_fact_parents(connection, code=f"organization-{violation}-first")
    if violation == "cross_school":
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="second school root violates the database anchor",
        ):
            _insert_school_fact_parents(
                connection,
                code=f"organization-{violation}-second",
            )
        return
    shape = _organization_relation_shape(connection, _foreign_key_shapes(connection))
    organization_column = _single_column(shape, "organization_units")
    campus_column = _single_column(shape, "campus_locations")

    legal = _required_insert_values(
        connection,
        table_name=shape.table_name,
        values={
            "school_id": first["school_id"],
            organization_column: first["organization_id"],
            campus_column: first["campus_id"],
            "source_id": first["source_id"],
        },
        row_code=f"organization-{violation}:legal",
    )
    _insert_dynamic_row(connection, table_name=shape.table_name, values=legal)

    invalid: dict[str, object] = {
        "school_id": first["school_id"],
        organization_column: first["second_organization_id"],
        campus_column: first["campus_id"],
        "source_id": first["source_id"],
    }
    if violation == "empty_interval":
        invalid.update({"valid_from": NOW, "valid_to": NOW})
    invalid = _required_insert_values(
        connection,
        table_name=shape.table_name,
        values=invalid,
        row_code=f"organization-{violation}:invalid",
    )
    with pytest.raises(psycopg.DataError) as exc_info, connection.transaction():
        _insert_dynamic_row(connection, table_name=shape.table_name, values=invalid)
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert exc_info.value.sqlstate == "22023"


@pytest.mark.integration
@pytest.mark.parametrize(
    "target",
    (
        "student_primary",
        "student_administrative",
        "policy_scope_policy",
        "policy_scope_organization",
        "policy_scope_campus",
    ),
)
def test_database_anchor_prevents_cross_school_relationship_fixtures(
    target: str,
    current_master_data_schema: PostgresRoles,
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    del current_master_data_schema
    connection = current_owner_connection
    _insert_school_fact_parents(connection, code=f"cross-school-{target}-first")
    with pytest.raises(
        psycopg.errors.CheckViolation,
        match="second school root violates the database anchor",
    ):
        _insert_school_fact_parents(connection, code=f"cross-school-{target}-second")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("interval_code", "valid_from", "valid_to"),
    (
        ("empty", NOW, NOW),
        ("reverse", NOW + timedelta(days=1), NOW),
    ),
)
def test_policy_scope_rejects_empty_or_reverse_effective_interval(
    interval_code: str,
    valid_from: datetime,
    valid_to: datetime,
    current_master_data_schema: PostgresRoles,
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    del current_master_data_schema
    connection = current_owner_connection
    ids = _insert_school_fact_parents(connection, code=f"policy-scope-{interval_code}")
    shape = _policy_scope_shape(connection, _foreign_key_shapes(connection))
    policy_target = _policy_target_table(shape)
    invalid = _required_insert_values(
        connection,
        table_name=shape.table_name,
        values={
            "school_id": ids["school_id"],
            _single_column(shape, policy_target): ids[
                "policy_version_id" if policy_target == "school_policy_versions" else "policy_id"
            ],
            _single_column(shape, "organization_units"): ids["organization_id"],
            _single_column(shape, "campus_locations"): None,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "source_id": ids["source_id"],
        },
        row_code=f"policy-scope-{interval_code}",
    )
    with pytest.raises(psycopg.IntegrityError), connection.transaction():
        _insert_dynamic_row(connection, table_name=shape.table_name, values=invalid)
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")


def _formal_target_id(harness, *, batch_id: UUID, fact_kind: str, source_key: str) -> UUID:
    with harness.factory() as session:  # type: ignore[operator]
        counts = session.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM master_data_import_units WHERE batch_id = :batch_id), "
                "(SELECT count(*) FROM school_fact_change_lineage WHERE batch_id = :batch_id)"
            ),
            {"batch_id": batch_id},
        ).one()
        target_id = session.execute(
            text(
                "SELECT target_id FROM school_fact_change_lineage "
                "WHERE batch_id = :batch_id AND fact_kind = :fact_kind "
                "AND fact_source_key = :source_key"
            ),
            {"batch_id": batch_id, "fact_kind": fact_kind, "source_key": source_key},
        ).scalar_one()
    assert counts == (1, 1)
    return UUID(str(target_id))


def _fact_row_values(
    harness, table: Table, target_id: UUID, *column_names: str
) -> tuple[object, ...]:
    assert "id" in table.c
    with harness.factory() as session:  # type: ignore[operator]
        return tuple(
            session.execute(
                select(*(table.c[name] for name in column_names)).where(table.c.id == target_id)
            ).one()
        )


@pytest.mark.integration
def test_organization_campus_relationship_reaches_formal_confirmation(
    current_master_data_schema: PostgresRoles,
) -> None:
    with _residence_harness(current_master_data_schema) as harness:
        source_key = "organization-campus:dorm-main"
        plan = _plan(
            label="formal-organization-campus",
            base_version=harness.version,
            changes=(
                {
                    "operation": "upsert",
                    "fact_kind": "organization_campus_relationship",
                    "source_key": source_key,
                    "expected_version": 0,
                    "effective_at": NOW,
                    "origin": {"sheet_code": "organization_campuses", "row_number": 2},
                    "content": {
                        "organization_source_key": "organization:dorm",
                        "campus_source_key": "location:campus",
                    },
                },
            ),
        )
        result = harness.confirm(plan, label="formal-organization-campus")
        target_id = _formal_target_id(
            harness,
            batch_id=result.batch_id,
            fact_kind="organization_campus_relationship",
            source_key=source_key,
        )
        table = _organization_campus_relation()
        organization_column = _local_reference_columns(table, "organization_units")[0]
        campus_column = _local_reference_columns(table, "campus_locations")[0]
        with harness.factory() as session:  # type: ignore[operator]
            expected = session.execute(
                text(
                    "SELECT organization.id, campus.id FROM organization_units AS organization, "
                    "campus_locations AS campus WHERE organization.school_id = :school_id "
                    "AND organization.source_key = 'organization:dorm' "
                    "AND campus.school_id = :school_id "
                    "AND campus.source_key = 'location:campus'"
                ),
                {"school_id": harness.school_id},
            ).one()
        assert _fact_row_values(
            harness, table, target_id, organization_column, campus_column
        ) == tuple(expected)


@pytest.mark.integration
@pytest.mark.parametrize("operation", ("end", "revoke"))
def test_organization_campus_historical_reader_distinguishes_end_from_revoke(
    isolated_migration_schema: PostgresRoles,
    operation: str,
) -> None:
    """A revoked slice never existed; an ended slice remains true before its boundary."""

    with _residence_harness(isolated_migration_schema) as harness:
        source_key = f"organization-campus:historical-{operation}"
        harness.confirm(
            _plan(
                label=f"organization-campus-historical-seed-{operation}",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": "organization_campus_relationship",
                        "source_key": source_key,
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {"sheet_code": "organization_campuses", "row_number": 2},
                        "content": {
                            "organization_source_key": "organization:dorm",
                            "campus_source_key": "location:campus",
                        },
                    },
                ),
            ),
            label=f"organization-campus-historical-seed-{operation}",
        )
        boundary = NOW + timedelta(days=1)
        terminal = harness.confirm(
            _plan(
                label=f"organization-campus-historical-{operation}",
                base_version=harness.version,
                changes=(
                    {
                        "operation": operation,
                        "fact_kind": "organization_campus_relationship",
                        "source_key": source_key,
                        "expected_version": 1,
                        "effective_at": boundary,
                        "origin": {"sheet_code": "organization_campuses", "row_number": 3},
                        "content": None,
                    },
                ),
            ),
            label=f"organization-campus-historical-{operation}",
        )

        before = harness.reader().find_organization_campus_ids(
            SchoolId(harness.school_id),
            "organization:dorm",
            NOW + timedelta(hours=12),
        )
        after = harness.reader().find_organization_campus_ids(
            SchoolId(harness.school_id),
            "organization:dorm",
            boundary + timedelta(seconds=1),
        )
        with harness.factory() as session:  # type: ignore[operator]
            stored_effective_at = session.execute(
                text(
                    "SELECT unit.payload->>'effective_at' "
                    "FROM master_data_import_units AS unit "
                    "WHERE unit.batch_id = :batch_id"
                ),
                {"batch_id": terminal.batch_id},
            ).scalar_one()

        if operation == "end":
            assert len(before) == 1
        else:
            assert before == (), "revoke 后的关系切片仍被历史 reader 当作成立过"
        assert after == ()
        assert stored_effective_at == boundary.isoformat().replace("+00:00", "Z")


@pytest.mark.integration
def test_organization_campus_history_uses_parent_content_slices_not_current_status(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Parent end preserves earlier truth; revoke/reactivate cannot revive an old slice."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    with _residence_harness(
        isolated_migration_schema,
        seed_effective_at=checked_now - timedelta(days=1),
        clock_at=checked_now,
    ) as harness:
        organization_key = "organization:historical-end"
        harness.confirm(
            _plan(
                label="organization-history-parent-seed",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": "organization",
                        "source_key": organization_key,
                        "expected_version": 0,
                        "effective_at": checked_now,
                        "origin": {"sheet_code": "organizations", "row_number": 2},
                        "content": {
                            "kind": "department",
                            "business_code": "HISTORY",
                            "display_name": "历史组织",
                            "display_path": "/历史组织",
                            "status": "active",
                        },
                    },
                ),
            ),
            label="organization-history-parent-seed",
        )
        harness.confirm(
            _plan(
                label="organization-history-relation-seed",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": "organization_campus_relationship",
                        "source_key": "organization-campus:historical-parent-end",
                        "expected_version": 0,
                        "effective_at": checked_now,
                        "origin": {"sheet_code": "organization_campuses", "row_number": 2},
                        "content": {
                            "organization_source_key": organization_key,
                            "campus_source_key": "location:campus",
                        },
                    },
                ),
            ),
            label="organization-history-relation-seed",
        )
        organization_end = checked_now + timedelta(days=2)
        harness.confirm(
            _plan(
                label="organization-history-parent-end",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "end",
                        "fact_kind": "organization",
                        "source_key": organization_key,
                        "expected_version": 1,
                        "effective_at": organization_end,
                        "origin": {"sheet_code": "organizations", "row_number": 3},
                        "content": None,
                    },
                ),
            ),
            label="organization-history-parent-end",
        )

        before_end = harness.reader().find_organization_campus_ids(
            SchoolId(harness.school_id),
            organization_key,
            organization_end - timedelta(hours=1),
        )
        after_end = harness.reader().find_organization_campus_ids(
            SchoolId(harness.school_id),
            organization_key,
            organization_end + timedelta(hours=1),
        )
        assert len(before_end) == 1, "组织后来 end 不得抹掉 end 边界前的校区归属"
        assert after_end == ()

        campus_key = "location:historical-revoke-reactivate"
        campus_content = {
            "kind": "campus",
            "business_code": "HISTORY-CAMPUS",
            "display_name": "历史校区",
            "display_path": "/历史校区",
            "status": "active",
        }
        harness.confirm(
            _plan(
                label="campus-history-parent-seed",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": "location",
                        "source_key": campus_key,
                        "expected_version": 0,
                        "effective_at": checked_now,
                        "origin": {"sheet_code": "locations", "row_number": 2},
                        "content": campus_content,
                    },
                ),
            ),
            label="campus-history-parent-seed",
        )
        harness.confirm(
            _plan(
                label="campus-history-relation-seed",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": "organization_campus_relationship",
                        "source_key": "organization-campus:historical-parent-revoke",
                        "expected_version": 0,
                        "effective_at": checked_now,
                        "origin": {"sheet_code": "organization_campuses", "row_number": 3},
                        "content": {
                            "organization_source_key": "organization:dorm",
                            "campus_source_key": campus_key,
                        },
                    },
                ),
            ),
            label="campus-history-relation-seed",
        )
        campus_revoke = checked_now + timedelta(days=3)
        harness.confirm(
            _plan(
                label="campus-history-parent-revoke",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "revoke",
                        "fact_kind": "location",
                        "source_key": campus_key,
                        "expected_version": 1,
                        "effective_at": campus_revoke,
                        "origin": {"sheet_code": "locations", "row_number": 4},
                        "content": None,
                    },
                ),
            ),
            label="campus-history-parent-revoke",
        )
        harness.confirm(
            _plan(
                label="campus-history-parent-reactivate",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "reactivate",
                        "fact_kind": "location",
                        "source_key": campus_key,
                        "expected_version": 2,
                        "effective_at": campus_revoke + timedelta(days=1),
                        "origin": {"sheet_code": "locations", "row_number": 5},
                        "content": campus_content,
                    },
                ),
            ),
            label="campus-history-parent-reactivate",
        )

        old_slice_after_reactivation = harness.reader().find_organization_campus_ids(
            SchoolId(harness.school_id),
            "organization:dorm",
            campus_revoke - timedelta(hours=1),
        )
        with harness.factory() as session:  # type: ignore[operator]
            revoked_campus_id = session.execute(
                text(
                    "SELECT id FROM campus_locations WHERE school_id = :school_id "
                    "AND source_key = :source_key"
                ),
                {"school_id": harness.school_id, "source_key": campus_key},
            ).scalar_one()
        assert revoked_campus_id not in old_slice_after_reactivation, (
            "校区根 revoke 后 reactivate 不得让旧关系切片在历史时点复活"
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fact_kind", "content_version"),
    ((None, 1), ("location", None)),
)
def test_historical_terminal_reader_rejects_null_key_arguments(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str | None,
    content_version: int | None,
) -> None:
    roles = isolated_migration_schema
    with psycopg.connect(
        **roles.migration_connection_parameters,
        autocommit=True,
    ) as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        with pytest.raises(psycopg.DataError) as exc_info:
            connection.execute(
                "SELECT * FROM app.school_fact_content_version_terminal("
                "%s::uuid, %s::text, %s::uuid, %s::bigint)",
                (uuid4(), fact_kind, uuid4(), content_version),
            ).fetchall()
    assert exc_info.value.sqlstate == "22023"


@pytest.mark.integration
def test_student_campuses_reach_formal_confirmation(
    current_master_data_schema: PostgresRoles,
) -> None:
    with _residence_harness(
        current_master_data_schema,
        include_initial_residence=False,
    ) as harness:
        source_key = "student-school:001"
        plan = _plan(
            label="formal-student-campuses",
            base_version=harness.version,
            changes=(
                {
                    "operation": "upsert",
                    "fact_kind": "student_school_relationship",
                    "source_key": source_key,
                    "expected_version": 1,
                    "effective_at": NOW + timedelta(days=1),
                    "origin": {"sheet_code": "student_school_relationships", "row_number": 2},
                    "content": {
                        "student_source_key": "student:001",
                        "student_number": "20260001",
                        "enrollment_status": "enrolled",
                        "residence_mode": "day",
                        "primary_campus_source_key": "location:campus",
                        "administrative_campus_source_key": "location:campus",
                    },
                },
            ),
        )
        result = harness.confirm(plan, label="formal-student-campuses")
        target_id = _formal_target_id(
            harness,
            batch_id=result.batch_id,
            fact_kind="student_school_relationship",
            source_key=source_key,
        )
        table = Base.metadata.tables["student_school_relationships"]
        campus_columns = _local_reference_columns(table, "campus_locations")
        primary = next(name for name in campus_columns if "primary" in name)
        administrative = next(name for name in campus_columns if "administrative" in name)
        with harness.factory() as session:  # type: ignore[operator]
            campus_id = session.execute(
                text(
                    "SELECT id FROM campus_locations WHERE school_id = :school_id "
                    "AND source_key = 'location:campus'"
                ),
                {"school_id": harness.school_id},
            ).scalar_one()
        assert _fact_row_values(harness, table, target_id, primary, administrative) == (
            campus_id,
            campus_id,
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("target_field", "target_source_key"),
    (
        ("campus_source_key", "location:campus"),
        ("organization_source_key", "organization:dorm"),
    ),
)
def test_policy_scope_reaches_formal_confirmation(
    target_field: str,
    target_source_key: str,
    current_master_data_schema: PostgresRoles,
) -> None:
    with _residence_harness(current_master_data_schema) as harness:
        source_key = f"policy-scope:{target_field}"
        plan = _plan(
            label=f"formal-policy-scope-{target_field}",
            base_version=harness.version,
            changes=(
                {
                    "operation": "upsert",
                    "fact_kind": "policy_scope",
                    "source_key": source_key,
                    "expected_version": 0,
                    "effective_at": NOW,
                    "origin": {"sheet_code": "policy_scopes", "row_number": 2},
                    "content": {
                        "policy_source_key": "policy:dorm",
                        target_field: target_source_key,
                    },
                },
            ),
        )
        result = harness.confirm(plan, label=f"formal-policy-scope-{target_field}")
        target_id = _formal_target_id(
            harness,
            batch_id=result.batch_id,
            fact_kind="policy_scope",
            source_key=source_key,
        )
        table = _policy_scope_relation()
        organization_column = _local_reference_columns(table, "organization_units")[0]
        campus_column = _local_reference_columns(table, "campus_locations")[0]
        organization_id, campus_id = _fact_row_values(
            harness, table, target_id, organization_column, campus_column
        )
        if target_field == "organization_source_key":
            assert organization_id is not None and campus_id is None
        else:
            assert organization_id is None and campus_id is not None


@pytest.mark.integration
@pytest.mark.parametrize(
    "fact_kind",
    ("organization_campus_relationship", "policy_scope"),
)
def test_effective_relation_upsert_creates_a_new_physical_slice_with_own_lineage(
    fact_kind: str,
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _residence_harness(isolated_migration_schema) as harness:
        source_key = f"{fact_kind}:versioned-slice"
        sibling_source_key = f"{fact_kind}:unrelated-slice"
        if fact_kind == "organization_campus_relationship":
            first_content = second_content = {
                "organization_source_key": "organization:dorm",
                "campus_source_key": "location:campus",
            }
            table = _organization_campus_relation()
        else:
            first_content = {
                "policy_source_key": "policy:dorm",
                "campus_source_key": "location:campus",
            }
            second_content = {
                "policy_source_key": "policy:dorm",
                "organization_source_key": "organization:dorm",
            }
            table = _policy_scope_relation()

        harness.confirm(
            _plan(
                label=f"{fact_kind}-second-campus",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": "location",
                        "source_key": f"location:second-campus:{fact_kind}",
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {"sheet_code": "locations", "row_number": 91},
                        "content": {
                            "kind": "campus",
                            "business_code": f"SECOND-{fact_kind}",
                            "display_name": f"{fact_kind} 第二校区",
                            "display_path": f"/{fact_kind} 第二校区",
                            "status": "active",
                        },
                    },
                ),
            ),
            label=f"{fact_kind}-second-campus",
        )
        sibling_content = {
            **first_content,
            "campus_source_key": f"location:second-campus:{fact_kind}",
        }

        sibling = harness.confirm(
            _plan(
                label=f"{fact_kind}-unrelated-slice",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": fact_kind,
                        "source_key": sibling_source_key,
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {"sheet_code": fact_kind, "row_number": 1},
                        "content": sibling_content,
                    },
                ),
            ),
            label=f"{fact_kind}-unrelated-slice",
        )
        sibling_target = _formal_target_id(
            harness,
            batch_id=sibling.batch_id,
            fact_kind=fact_kind,
            source_key=sibling_source_key,
        )

        first = harness.confirm(
            _plan(
                label=f"{fact_kind}-slice-1",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": fact_kind,
                        "source_key": source_key,
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {"sheet_code": fact_kind, "row_number": 2},
                        "content": first_content,
                    },
                ),
            ),
            label=f"{fact_kind}-slice-1",
        )
        first_target = _formal_target_id(
            harness,
            batch_id=first.batch_id,
            fact_kind=fact_kind,
            source_key=source_key,
        )
        boundary = NOW + timedelta(days=1)
        second = harness.confirm(
            _plan(
                label=f"{fact_kind}-slice-2",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": fact_kind,
                        "source_key": source_key,
                        "expected_version": 1,
                        "effective_at": boundary,
                        "origin": {"sheet_code": fact_kind, "row_number": 3},
                        "content": second_content,
                    },
                ),
            ),
            label=f"{fact_kind}-slice-2",
        )
        second_target = _formal_target_id(
            harness,
            batch_id=second.batch_id,
            fact_kind=fact_kind,
            source_key=source_key,
        )

        assert second_target != first_target
        assert _fact_row_values(harness, table, first_target, "valid_from", "valid_to") == (
            NOW,
            boundary,
        )
        assert _fact_row_values(harness, table, second_target, "valid_from", "valid_to") == (
            boundary,
            None,
        )
        assert _fact_row_values(
            harness,
            table,
            sibling_target,
            "valid_from",
            "valid_to",
        ) == (NOW, None)
        with harness.factory() as session:  # type: ignore[operator]
            lineage = session.execute(
                text(
                    "SELECT target_id, target_version, operation "
                    "FROM school_fact_change_lineage "
                    "WHERE school_id = :school_id AND fact_kind = :fact_kind "
                    "AND fact_source_key = :source_key ORDER BY target_version"
                ),
                {
                    "school_id": harness.school_id,
                    "fact_kind": fact_kind,
                    "source_key": source_key,
                },
            ).all()
        assert lineage == [(first_target, 1, "upsert"), (second_target, 2, "upsert")]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fact_kind", "content"),
    (
        (
            "organization_campus_relationship",
            {
                "organization_source_key": "organization:dorm",
                "campus_source_key": "location:campus",
            },
        ),
        (
            "policy_scope",
            {
                "policy_source_key": "policy:dorm",
                "campus_source_key": "location:campus",
            },
        ),
    ),
)
def test_terminated_effective_relation_rejects_backdated_same_source_upsert(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
    content: dict[str, object],
) -> None:
    """A backdated effective_at cannot rewrite the terminal row after lineage says end."""

    with _residence_harness(isolated_migration_schema) as harness:
        source_key = f"{fact_kind}:terminal-backdate"
        harness.confirm(
            _plan(
                label=f"{fact_kind}-terminal-backdate-seed",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": fact_kind,
                        "source_key": source_key,
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {"sheet_code": fact_kind, "row_number": 2},
                        "content": content,
                    },
                ),
            ),
            label=f"{fact_kind}-terminal-backdate-seed",
        )
        terminal_at = NOW + timedelta(days=2)
        harness.confirm(
            _plan(
                label=f"{fact_kind}-terminal-backdate-end",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "end",
                        "fact_kind": fact_kind,
                        "source_key": source_key,
                        "expected_version": 1,
                        "effective_at": terminal_at,
                        "origin": {"sheet_code": fact_kind, "row_number": 3},
                        "content": None,
                    },
                ),
            ),
            label=f"{fact_kind}-terminal-backdate-end",
        )
        table_name = (
            "organization_campus_relationships"
            if fact_kind == "organization_campus_relationship"
            else "school_policy_scopes"
        )
        with harness.factory() as session:  # type: ignore[operator]
            before_rows = session.execute(
                text(
                    f"SELECT id, valid_from, valid_to, version FROM {table_name} "  # noqa: S608
                    "WHERE school_id = :school_id AND source_key = :source_key "
                    "ORDER BY version"
                ),
                {"school_id": harness.school_id, "source_key": source_key},
            ).all()

        with pytest.raises(SchoolFactChangeRejected):
            harness.confirm(
                _plan(
                    label=f"{fact_kind}-terminal-backdate-bypass",
                    base_version=harness.version,
                    changes=(
                        {
                            "operation": "upsert",
                            "fact_kind": fact_kind,
                            "source_key": source_key,
                            "expected_version": 2,
                            "effective_at": NOW + timedelta(days=1),
                            "origin": {"sheet_code": fact_kind, "row_number": 4},
                            "content": content,
                        },
                    ),
                ),
                label=f"{fact_kind}-terminal-backdate-bypass",
            )

        with harness.factory() as session:  # type: ignore[operator]
            after_rows = session.execute(
                text(
                    f"SELECT id, valid_from, valid_to, version FROM {table_name} "  # noqa: S608
                    "WHERE school_id = :school_id AND source_key = :source_key "
                    "ORDER BY version"
                ),
                {"school_id": harness.school_id, "source_key": source_key},
            ).all()
            lineage_versions = session.execute(
                text(
                    "SELECT target_version, operation FROM school_fact_change_lineage "
                    "WHERE school_id = :school_id AND fact_kind = :fact_kind "
                    "AND fact_source_key = :source_key ORDER BY target_version"
                ),
                {
                    "school_id": harness.school_id,
                    "fact_kind": fact_kind,
                    "source_key": source_key,
                },
            ).all()
        assert after_rows == before_rows
        assert lineage_versions == [(1, "upsert"), (2, "end")]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fact_kind", "content", "table_name"),
    (
        (
            "organization_campus_relationship",
            {
                "organization_source_key": "organization:dorm",
                "campus_source_key": "location:campus",
            },
            "organization_campus_relationships",
        ),
        (
            "policy_scope",
            {
                "policy_source_key": "policy:dorm",
                "campus_source_key": "location:campus",
            },
            "school_policy_scopes",
        ),
    ),
)
def test_terminal_effective_relation_rejects_a_second_terminal_operation(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
    content: dict[str, object],
    table_name: str,
) -> None:
    """A later terminal cannot rewrite the first terminal boundary or reader result."""

    with _residence_harness(isolated_migration_schema) as harness:
        source_key = f"{fact_kind}:repeat-terminal"
        harness.confirm(
            _plan(
                label=f"{fact_kind}-repeat-terminal-seed",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": fact_kind,
                        "source_key": source_key,
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {"sheet_code": fact_kind, "row_number": 2},
                        "content": content,
                    },
                ),
            ),
            label=f"{fact_kind}-repeat-terminal-seed",
        )
        first_terminal_at = NOW + timedelta(days=2)
        harness.confirm(
            _plan(
                label=f"{fact_kind}-repeat-terminal-first",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "end",
                        "fact_kind": fact_kind,
                        "source_key": source_key,
                        "expected_version": 1,
                        "effective_at": first_terminal_at,
                        "origin": {"sheet_code": fact_kind, "row_number": 3},
                        "content": None,
                    },
                ),
            ),
            label=f"{fact_kind}-repeat-terminal-first",
        )
        with harness.factory() as session:  # type: ignore[operator]
            before = session.execute(
                text(
                    f"SELECT valid_to FROM {table_name} "  # noqa: S608
                    "WHERE school_id = :school_id AND source_key = :source_key"
                ),
                {"school_id": harness.school_id, "source_key": source_key},
            ).scalar_one()

        with pytest.raises(SchoolFactChangeRejected):
            harness.confirm(
                _plan(
                    label=f"{fact_kind}-repeat-terminal-second",
                    base_version=harness.version,
                    changes=(
                        {
                            "operation": "revoke",
                            "fact_kind": fact_kind,
                            "source_key": source_key,
                            "expected_version": 2,
                            "effective_at": first_terminal_at + timedelta(days=1),
                            "origin": {"sheet_code": fact_kind, "row_number": 4},
                            "content": None,
                        },
                    ),
                ),
                label=f"{fact_kind}-repeat-terminal-second",
            )

        with harness.factory() as session:  # type: ignore[operator]
            after = session.execute(
                text(
                    f"SELECT valid_to FROM {table_name} "  # noqa: S608
                    "WHERE school_id = :school_id AND source_key = :source_key"
                ),
                {"school_id": harness.school_id, "source_key": source_key},
            ).scalar_one()
            lineage = session.execute(
                text(
                    "SELECT target_version, operation FROM school_fact_change_lineage "
                    "WHERE school_id = :school_id AND fact_kind = :fact_kind "
                    "AND fact_source_key = :source_key ORDER BY target_version"
                ),
                {
                    "school_id": harness.school_id,
                    "fact_kind": fact_kind,
                    "source_key": source_key,
                },
            ).all()
        assert before == after == first_terminal_at
        assert lineage == [(1, "upsert"), (2, "end")]
        if fact_kind == "organization_campus_relationship":
            assert harness.reader().find_organization_campus_ids(
                SchoolId(harness.school_id),
                "organization:dorm",
                first_terminal_at - timedelta(seconds=1),
            )
            assert (
                harness.reader().find_organization_campus_ids(
                    SchoolId(harness.school_id),
                    "organization:dorm",
                    first_terminal_at,
                )
                == ()
            )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fact_kind", "content", "table_name"),
    (
        (
            "organization_campus_relationship",
            {
                "organization_source_key": "organization:dorm",
                "campus_source_key": "location:campus",
            },
            "organization_campus_relationships",
        ),
        (
            "policy_scope",
            {
                "policy_source_key": "policy:dorm",
                "campus_source_key": "location:campus",
            },
            "school_policy_scopes",
        ),
    ),
)
def test_end_cannot_extend_an_existing_bounded_effective_relation(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
    content: dict[str, object],
    table_name: str,
) -> None:
    """end may shorten an interval, but must never extend its declared endpoint."""

    with _residence_harness(isolated_migration_schema) as harness:
        source_key = f"{fact_kind}:bounded-end"
        planned_end = NOW + timedelta(days=2)
        harness.confirm(
            _plan(
                label=f"{fact_kind}-bounded-end-seed",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": fact_kind,
                        "source_key": source_key,
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {"sheet_code": fact_kind, "row_number": 2},
                        "content": {**content, "valid_to": planned_end},
                    },
                ),
            ),
            label=f"{fact_kind}-bounded-end-seed",
        )

        with pytest.raises(SchoolFactChangeRejected):
            harness.confirm(
                _plan(
                    label=f"{fact_kind}-bounded-end-extend",
                    base_version=harness.version,
                    changes=(
                        {
                            "operation": "end",
                            "fact_kind": fact_kind,
                            "source_key": source_key,
                            "expected_version": 1,
                            "effective_at": planned_end + timedelta(days=1),
                            "origin": {"sheet_code": fact_kind, "row_number": 3},
                            "content": None,
                        },
                    ),
                ),
                label=f"{fact_kind}-bounded-end-extend",
            )

        with harness.factory() as session:  # type: ignore[operator]
            stored_end = session.execute(
                text(
                    f"SELECT valid_to FROM {table_name} "  # noqa: S608
                    "WHERE school_id = :school_id AND source_key = :source_key"
                ),
                {"school_id": harness.school_id, "source_key": source_key},
            ).scalar_one()
            lineage = session.execute(
                text(
                    "SELECT target_version, operation FROM school_fact_change_lineage "
                    "WHERE school_id = :school_id AND fact_kind = :fact_kind "
                    "AND fact_source_key = :source_key ORDER BY target_version"
                ),
                {
                    "school_id": harness.school_id,
                    "fact_kind": fact_kind,
                    "source_key": source_key,
                },
            ).all()
        assert stored_end == planned_end
        assert lineage == [(1, "upsert")]


@pytest.mark.integration
def test_policy_scope_formally_allows_a_non_null_scope_organization_different_from_publisher(
    current_master_data_schema: PostgresRoles,
) -> None:
    with _residence_harness(current_master_data_schema) as harness:
        scope_organization_source_key = "organization:policy-scope-other"
        harness.confirm(
            _plan(
                label="add-policy-scope-organization",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": "organization",
                        "source_key": scope_organization_source_key,
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {"sheet_code": "organizations", "row_number": 3},
                        "content": {
                            "kind": "department",
                            "business_code": "POLICY-SCOPE",
                            "display_name": "制度适用处",
                            "display_path": "/制度适用处",
                            "status": "active",
                        },
                    },
                ),
            ),
            label="add-policy-scope-organization",
        )

        source_key = "policy-scope:publisher-and-scope-differ"
        plan = _plan(
            label="formal-policy-scope-publisher-and-scope-differ",
            base_version=harness.version,
            changes=(
                {
                    "operation": "upsert",
                    "fact_kind": "policy_scope",
                    "source_key": source_key,
                    "expected_version": 0,
                    "effective_at": NOW,
                    "origin": {"sheet_code": "policy_scopes", "row_number": 3},
                    "content": {
                        "policy_source_key": "policy:dorm",
                        "organization_source_key": scope_organization_source_key,
                    },
                },
            ),
        )
        result = harness.confirm(plan, label="formal-policy-scope-publisher-and-scope-differ")
        target_id = _formal_target_id(
            harness,
            batch_id=result.batch_id,
            fact_kind="policy_scope",
            source_key=source_key,
        )
        scope_relation = _policy_scope_relation()
        scope_organization_column = _local_reference_columns(scope_relation, "organization_units")[
            0
        ]
        scope_organization_id = _fact_row_values(
            harness, scope_relation, target_id, scope_organization_column
        )[0]
        with harness.factory() as session:  # type: ignore[operator]
            publisher_organization_id = session.execute(
                text(
                    "SELECT version.publishing_organization_unit_id "
                    "FROM school_policy_versions AS version "
                    "JOIN school_policies AS policy ON policy.id = version.policy_id "
                    "WHERE version.school_id = :school_id "
                    "AND policy.source_key = 'policy:dorm'"
                ),
                {"school_id": harness.school_id},
            ).scalar_one()
            expected_scope_organization_id = session.execute(
                text(
                    "SELECT id FROM organization_units WHERE school_id = :school_id "
                    "AND source_key = :source_key"
                ),
                {
                    "school_id": harness.school_id,
                    "source_key": scope_organization_source_key,
                },
            ).scalar_one()
        assert publisher_organization_id is not None
        assert scope_organization_id == expected_scope_organization_id
        assert scope_organization_id is not None
        assert scope_organization_id != publisher_organization_id


def _policy_identity_values(
    shape: _ForeignKeyShape,
    ids: dict[str, UUID],
    *,
    ordinal: str,
) -> dict[str, object]:
    target = next(
        name
        for name in ("school_policy_versions", "school_policies")
        if name in shape.columns_by_target
    )
    identities = {
        "first": {
            "school_policies": ids["policy_id"],
            "school_policy_versions": ids["policy_version_id"],
        },
        "second": {
            "school_policies": ids["second_policy_id"],
            "school_policy_versions": ids["second_policy_version_id"],
        },
        "third": {
            "school_policies": ids["third_policy_id"],
            "school_policy_versions": ids["third_policy_version_id"],
        },
        "fourth": {
            "school_policies": ids["fourth_policy_id"],
            "school_policy_versions": ids["fourth_policy_version_id"],
        },
    }
    return {_single_column(shape, target): identities[ordinal][target]}


@pytest.mark.integration
def test_policy_scope_postgresql_enforces_exactly_one_target(
    current_master_data_schema: PostgresRoles,
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    del current_master_data_schema
    connection = current_owner_connection
    ids = _insert_school_fact_parents(connection, code="policy-exactly-one")
    policy_parent_ids = (
        ids["policy_id"],
        ids["second_policy_id"],
        ids["third_policy_id"],
        ids["fourth_policy_id"],
    )
    policy_version_parent_ids = (
        ids["policy_version_id"],
        ids["second_policy_version_id"],
        ids["third_policy_version_id"],
        ids["fourth_policy_version_id"],
    )
    assert connection.execute(
        "SELECT count(*) FROM school_policies WHERE id = ANY(%s)",
        (list(policy_parent_ids),),
    ).fetchone() == (4,)
    assert connection.execute(
        "SELECT count(*) FROM school_policy_versions WHERE id = ANY(%s)",
        (list(policy_version_parent_ids),),
    ).fetchone() == (4,)
    shape = _policy_scope_shape(connection, _foreign_key_shapes(connection))
    campus_column = _single_column(shape, "campus_locations")
    organization_column = _single_column(shape, "organization_units")

    legal_campus = _required_insert_values(
        connection,
        table_name=shape.table_name,
        values={
            "school_id": ids["school_id"],
            **_policy_identity_values(shape, ids, ordinal="first"),
            campus_column: ids["campus_id"],
            organization_column: None,
            "source_id": ids["source_id"],
        },
        row_code="policy-exactly-one:campus",
    )
    _insert_dynamic_row(connection, table_name=shape.table_name, values=legal_campus)

    legal_organization = _required_insert_values(
        connection,
        table_name=shape.table_name,
        values={
            "school_id": ids["school_id"],
            **_policy_identity_values(shape, ids, ordinal="second"),
            campus_column: None,
            organization_column: ids["organization_id"],
            "source_id": ids["source_id"],
        },
        row_code="policy-exactly-one:organization",
    )
    _insert_dynamic_row(connection, table_name=shape.table_name, values=legal_organization)

    invalid_targets = (
        (
            "neither",
            "third",
            {campus_column: None, organization_column: None},
        ),
        (
            "both",
            "fourth",
            {
                campus_column: ids["campus_id"],
                organization_column: ids["organization_id"],
            },
        ),
    )
    for row_code, ordinal, targets in invalid_targets:
        invalid = _required_insert_values(
            connection,
            table_name=shape.table_name,
            values={
                "school_id": ids["school_id"],
                **_policy_identity_values(shape, ids, ordinal=ordinal),
                **targets,
                "source_id": ids["source_id"],
            },
            row_code=f"policy-exactly-one:{row_code}",
        )
        with pytest.raises(psycopg.IntegrityError), connection.transaction():
            _insert_dynamic_row(
                connection,
                table_name=shape.table_name,
                values=invalid,
            )
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
