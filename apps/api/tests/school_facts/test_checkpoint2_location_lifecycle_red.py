from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.composition.school_fact_persistence import (
    SchoolFactPersistenceError,
    SqlAlchemySchoolFactPersistence,
)
from app.domains.campus.repository import SqlAlchemyCampusRepository
from app.platform.contracts import LocationId, SchoolId
from app.platform.school_facts.application import SchoolFactChangeRejected
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_checkpoint2_campus_policy_postgres import (
    _run_competing_owner_transactions,
)
from tests.school_facts.test_checkpoint2_campus_policy_postgres import (
    test_organization_campus_relationship_reaches_formal_confirmation as _org_campus_confirm_test,
)
from tests.school_facts.test_checkpoint2_campus_policy_postgres import (
    test_policy_scope_reaches_formal_confirmation as _policy_scope_confirmation_test,
)
from tests.school_facts.test_checkpoint2_campus_policy_postgres import (
    test_student_campuses_reach_formal_confirmation as _student_campus_confirmation_test,
)
from tests.school_facts.test_checkpoint2_lifecycle_red import (
    _confirm,
    _plan,
    _terminal_change,
)
from tests.school_facts.test_checkpoint2_lifecycle_red import (
    test_assignment_revoke_preserves_planned_valid_to as _assignment_revoke_test,
)
from tests.school_facts.test_checkpoint2_lifecycle_red import (
    test_terminal_root_facts_reject_ordinary_upsert_and_keep_stable_ids as _root_lifecycle_test,
)
from tests.school_facts.test_checkpoint2_residence_red import (
    _residence_change,
    _residence_harness,
)
from tests.school_facts.test_checkpoint2_residence_red import (
    test_a_to_b_to_c_uses_formal_channel_with_four_transfer_units_and_lineage as _a_to_b_to_c_test,
)
from tests.school_facts.test_checkpoint2_residence_red import (
    test_same_bed_continuous_slice_is_two_units_two_lineage_one_confirmation as _same_bed_test,
)
from tests.school_facts.test_sf2_transaction import NOW


def _stop_campus(
    harness,
    *,
    label: str,
    stopped_at: datetime | None = None,
    operation: str = "revoke",
):
    effective_stopped_at = stopped_at or NOW + timedelta(days=1)
    terminal_change = _terminal_change(
        "location",
        "location:campus",
        1,
        effective_stopped_at,
        2,
    )
    terminal_change["operation"] = operation
    plan = _plan(
        label=label,
        base_version=harness.version,
        changes=(terminal_change,),
    )
    result = harness.confirm(plan, label=label)
    return effective_stopped_at, result


def _historical_location_snapshot(
    harness,
    *,
    location_id: LocationId,
    as_of: datetime,
    label: str,
):
    del label
    with harness.factory() as session:  # type: ignore[operator]
        return SqlAlchemyCampusRepository(session).resolve_location_snapshot(
            SchoolId(harness.school_id),
            location_id,
            as_of=as_of,
        )


def test_current_location_ancestor_walk_is_cycle_safe() -> None:
    source = "\n".join(
        (
            inspect.getsource(SqlAlchemySchoolFactPersistence._current_reference_source_keys),
            inspect.getsource(
                SqlAlchemySchoolFactPersistence._validate_location_terminal_residences
            ),
        )
    )
    assert "school_fact_location_history_interval_is_resolvable" in source
    assert "WITH RECURSIVE" not in source
    assert "campus_locations AS child" not in source


def test_location_terminal_precheck_uses_versioned_interval_history() -> None:
    source = inspect.getsource(
        SqlAlchemySchoolFactPersistence._validate_location_terminal_residences
    )
    assert "school_fact_location_history_interval_is_resolvable" in source
    assert "stopped_locations" not in source


def test_child_location_is_not_current_after_campus_ancestor_stops(
    current_master_data_schema: PostgresRoles,
) -> None:
    """An active leaf cannot bypass an inactive ancestor in a current-fact reader."""

    with _residence_harness(current_master_data_schema, include_initial_residence=False) as harness:
        with harness.factory() as session:  # type: ignore[operator]
            child_id, child_name = session.execute(
                text(
                    "SELECT location.id, version.display_name "
                    "FROM campus_locations AS location "
                    "JOIN campus_location_versions AS version "
                    "ON version.school_id = location.school_id "
                    "AND version.location_id = location.id "
                    "WHERE location.school_id = :school_id "
                    "AND location.source_key = 'location:room-101' "
                    "AND version.valid_to IS NULL"
                ),
                {"school_id": harness.school_id},
            ).one()
        stopped_at, _result = _stop_campus(harness, label="stop-campus-for-child-reader")
        with harness.factory() as session:  # type: ignore[operator]
            repository = SqlAlchemyCampusRepository(session)
            current = repository.get_current_location(
                SchoolId(harness.school_id),
                LocationId(child_id),
                as_of=stopped_at + timedelta(seconds=1),
            )
            exact = repository.find_exact_locations(
                SchoolId(harness.school_id),
                location_text=str(child_name),
                as_of=stopped_at + timedelta(seconds=1),
            )
        assert current is None, "校区根已停用，但 current location reader 仍把其子地点视为有效"
        assert exact == (), "校区根已停用，但 exact current reader 仍返回其子地点"


@pytest.mark.parametrize("operation", ("end", "revoke"))
def test_location_historical_reader_distinguishes_end_from_revoke(
    isolated_migration_schema: PostgresRoles,
    operation: str,
) -> None:
    """Root terminal lineage governs historical truth independently of current status."""

    with _residence_harness(isolated_migration_schema, include_initial_residence=False) as harness:
        with harness.factory() as session:  # type: ignore[operator]
            location_id = LocationId(
                session.execute(
                    text(
                        "SELECT id FROM campus_locations "
                        "WHERE school_id = :school_id AND source_key = 'location:room-101'"
                    ),
                    {"school_id": harness.school_id},
                ).scalar_one()
            )
        boundary = NOW + timedelta(days=1)
        terminal = harness.confirm(
            _plan(
                label=f"location-historical-{operation}",
                base_version=harness.version,
                changes=(
                    {
                        "operation": operation,
                        "fact_kind": "location",
                        "source_key": "location:room-101",
                        "expected_version": 1,
                        "effective_at": boundary,
                        "origin": {"sheet_code": "location", "row_number": 2},
                        "content": None,
                    },
                ),
            ),
            label=f"location-historical-{operation}",
        )
        before = _historical_location_snapshot(
            harness,
            location_id=location_id,
            as_of=NOW + timedelta(hours=12),
            label=f"{operation}-before",
        )
        after = _historical_location_snapshot(
            harness,
            location_id=location_id,
            as_of=boundary + timedelta(seconds=1),
            label=f"{operation}-after",
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
            assert before is not None
        else:
            assert before is None, "revoke 后的地点版本仍被历史 reader 当作成立过"
        assert after is None
        assert stored_effective_at == boundary.isoformat().replace("+00:00", "Z")

        if operation == "revoke":
            reactivated_at = boundary + timedelta(days=1)
            with harness.factory() as session:  # type: ignore[operator]
                stable_status = session.execute(
                    text(
                        "SELECT status, version FROM campus_locations "
                        "WHERE school_id = :school_id AND id = :location_id"
                    ),
                    {"school_id": harness.school_id, "location_id": location_id},
                ).one()
            assert tuple(stable_status) == ("inactive", 2)
            harness.service = type(harness.service)(
                reader=harness.persistence,
                writer=harness.persistence,
                clock=lambda: datetime.now(UTC),
            )
            harness.confirm(
                _plan(
                    label="location-historical-reactivate",
                    base_version=harness.version,
                    changes=(
                        {
                            "operation": "reactivate",
                            "fact_kind": "location",
                            "source_key": "location:room-101",
                            "expected_version": 2,
                            "effective_at": reactivated_at,
                            "origin": {"sheet_code": "location", "row_number": 3},
                            "content": {
                                "parent_source_key": "location:campus",
                                "kind": "dorm_room",
                                "business_code": "A-101",
                                "display_name": "A栋101",
                                "display_path": "/A栋/101",
                                "status": "active",
                            },
                        },
                    ),
                ),
                label="location-historical-reactivate",
            )
            old_after_reactivation = _historical_location_snapshot(
                harness,
                location_id=location_id,
                as_of=NOW + timedelta(hours=12),
                label="revoke-reactivated-old",
            )
            current_after_reactivation = _historical_location_snapshot(
                harness,
                location_id=location_id,
                as_of=reactivated_at + timedelta(seconds=1),
                label="revoke-reactivated-current",
            )
            assert old_after_reactivation is None, "reactivate 不得让旧 revoke 切片复活"
            assert current_after_reactivation is not None
            assert current_after_reactivation.source_version == 3


def test_existing_room_and_bed_references_are_not_current_after_campus_ancestor_stops(
    current_master_data_schema: PostgresRoles,
) -> None:
    """Formal bulk preflight cannot resolve room/bed facts under an inactive campus."""

    with _residence_harness(current_master_data_schema, include_initial_residence=False) as harness:
        stopped_at, _result = _stop_campus(harness, label="stop-campus-for-room-bed-reader")
        changes = _plan(
            label="probe-room-bed-current-references",
            base_version=harness.version,
            changes=(
                {
                    "operation": "upsert",
                    "fact_kind": "dorm_room",
                    "source_key": "dorm-room:101",
                    "expected_version": 1,
                    "effective_at": stopped_at + timedelta(seconds=1),
                    "origin": {"sheet_code": "dorm_rooms", "row_number": 2},
                    "content": {
                        "room_source_key": "location:room-101",
                        "authorized_bed_capacity": 4,
                        "accommodation_zone": "boys",
                        "is_available": True,
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "dorm_bed",
                    "source_key": "dorm-bed:101-1",
                    "expected_version": 1,
                    "effective_at": stopped_at + timedelta(seconds=1),
                    "origin": {"sheet_code": "dorm_beds", "row_number": 3},
                    "content": {
                        "room_source_key": "location:room-101",
                        "business_code": "A-101-1",
                        "display_name": "1号床",
                        "availability": "available",
                    },
                },
                _residence_change(
                    operation="upsert",
                    source_key="residence:inactive-campus-reference-probe",
                    expected_version=0,
                    effective_at=stopped_at + timedelta(seconds=1),
                    row_number=4,
                    bed_source_key="dorm-bed:101-1",
                ),
            ),
        ).changes
        unresolved = harness.reader().unresolved_reference_codes_bulk(
            SchoolId(harness.school_id), changes
        )

        assert unresolved == {
            ("dorm_room", "dorm-room:101"): ("room_source_key",),
            ("dorm_bed", "dorm-bed:101-1"): ("room_source_key",),
            ("student_bed_residence", "residence:inactive-campus-reference-probe"): (
                "bed_source_key",
            ),
        }, (
            "校区根停用后，正式 bulk preflight 仍把其下 room location、dorm room "
            f"和 dorm bed 引用视为 current：{unresolved!r}"
        )


def _is_integrity_rejection(exc: SchoolFactPersistenceError) -> bool:
    db_error = exc.__context__
    original = getattr(db_error, "orig", None)
    sqlstate = getattr(original, "sqlstate", None)
    return isinstance(sqlstate, str) and sqlstate.startswith("23")


def test_new_residence_under_inactive_campus_cannot_become_effective(
    current_master_data_schema: PostgresRoles,
) -> None:
    """The invariant may be enforced in preflight, the writer, or PostgreSQL."""

    with _residence_harness(current_master_data_schema, include_initial_residence=False) as harness:
        stopped_at, _result = _stop_campus(harness, label="stop-campus-before-residence")
        attempted_at = stopped_at + timedelta(days=1)
        plan = _plan(
            label="residence-under-inactive-campus",
            base_version=harness.version,
            changes=(
                _residence_change(
                    operation="upsert",
                    source_key="residence:inactive-campus",
                    expected_version=0,
                    effective_at=attempted_at,
                    row_number=2,
                    bed_source_key="dorm-bed:101-1",
                ),
            ),
        )
        try:
            harness.confirm(plan, label="residence-under-inactive-campus")
        except SchoolFactChangeRejected:
            pass
        except SchoolFactPersistenceError as exc:
            assert _is_integrity_rejection(exc), (
                "只有 PostgreSQL 完整性拒绝可作为校区失效门；不得吞掉其他持久化错误"
            )

        with harness.factory() as session:  # type: ignore[operator]
            effective_count = int(
                session.execute(
                    text(
                        "SELECT count(*) FROM school_fact_change_lineage AS lineage "
                        "JOIN student_bed_residences AS residence "
                        "ON residence.id = lineage.target_id "
                        "WHERE lineage.school_id = :school_id "
                        "AND lineage.fact_kind = 'student_bed_residence' "
                        "AND lineage.fact_source_key = 'residence:inactive-campus' "
                        "AND residence.valid_from <= :as_of "
                        "AND (residence.valid_to IS NULL OR residence.valid_to > :as_of) "
                        "AND residence.revoked_at IS NULL"
                    ),
                    {"school_id": harness.school_id, "as_of": attempted_at},
                ).scalar_one()
            )
        assert effective_count == 0, "已停用校区下的新入住仍可经正式通道成为有效事实"


@pytest.mark.parametrize(
    ("operation", "label", "valid_from_delta", "valid_to_delta", "accepted"),
    (
        ("end", "bounded-current", timedelta(days=-1), timedelta(days=1), False),
        ("end", "future-open", timedelta(days=1), None, False),
        ("end", "past-ended", timedelta(days=-1), timedelta(hours=-23), True),
        ("end", "exact-boundary", timedelta(days=-1), timedelta(0), True),
        ("revoke", "bounded-current", timedelta(days=-1), timedelta(days=1), False),
        ("revoke", "future-open", timedelta(days=1), None, False),
        ("revoke", "past-ended", timedelta(days=-1), timedelta(hours=-23), False),
        ("revoke", "exact-boundary", timedelta(days=-1), timedelta(0), False),
    ),
)
def test_inactive_campus_residence_guard_is_temporally_bounded(
    isolated_migration_schema: PostgresRoles,
    operation: str,
    label: str,
    valid_from_delta: timedelta,
    valid_to_delta: timedelta | None,
    accepted: bool,
) -> None:
    """End preserves bounded history; revoke makes every related assertion unestablished."""

    with _residence_harness(isolated_migration_schema, include_initial_residence=False) as harness:
        with harness.factory() as session:  # type: ignore[operator]
            database_now = session.execute(text("SELECT statement_timestamp()")).scalar_one()
        stopped_at, _result = _stop_campus(
            harness,
            label=f"stop-campus-for-{label}",
            stopped_at=database_now,
            operation=operation,
        )
        valid_from = stopped_at + valid_from_delta
        valid_to = None if valid_to_delta is None else stopped_at + valid_to_delta
        source_key = f"residence:inactive-campus:{operation}:{label}"
        plan = _plan(
            label=f"inactive-campus-residence-{label}",
            base_version=harness.version,
            changes=(
                _residence_change(
                    operation="upsert",
                    source_key=source_key,
                    expected_version=0,
                    effective_at=valid_from,
                    row_number=2,
                    bed_source_key="dorm-bed:101-1",
                    valid_to=valid_to,
                ),
            ),
        )

        result = None
        rejection: Exception | None = None
        try:
            result = harness.confirm(plan, label=f"inactive-campus-residence-{label}")
        except (SchoolFactChangeRejected, SchoolFactPersistenceError) as exc:
            rejection = exc
            if isinstance(exc, SchoolFactPersistenceError):
                assert _is_integrity_rejection(exc)
        if accepted:
            assert rejection is None, (
                "已结束的历史入住被停用校区终态门拒绝："
                f"{rejection!r}; db={getattr(rejection, '__context__', None)!r}"
            )
            assert result is not None
            assert result.changed_count == 1
        else:
            assert rejection is not None, f"{label} 入住跨过校区停用边界却被接受"

        with harness.factory() as session:  # type: ignore[operator]
            rows = session.execute(
                text(
                    "SELECT residence.valid_from, residence.valid_to, count(lineage.id) "
                    "FROM school_fact_change_lineage AS lineage "
                    "JOIN student_bed_residences AS residence "
                    "ON residence.id = lineage.target_id "
                    "WHERE lineage.school_id = :school_id "
                    "AND lineage.fact_kind = 'student_bed_residence' "
                    "AND lineage.fact_source_key = :source_key "
                    "GROUP BY residence.valid_from, residence.valid_to"
                ),
                {"school_id": harness.school_id, "source_key": source_key},
            ).all()
        if accepted:
            assert rows == [(valid_from, valid_to, 1)]
        else:
            assert rows == []


@pytest.mark.integration
@pytest.mark.parametrize(
    ("terminal_campus", "operation"),
    (
        ("old", "end"),
        ("old", "revoke"),
        ("middle", "end"),
        ("middle", "revoke"),
    ),
)
def test_residence_interval_checks_every_historical_ancestor_slice(
    isolated_migration_schema: PostgresRoles,
    terminal_campus: str,
    operation: str,
) -> None:
    """A terminal campus is related when any intersecting parent slice reaches it."""

    with _residence_harness(isolated_migration_schema, include_initial_residence=False) as harness:
        second_at = NOW + timedelta(days=2)
        third_at = NOW + timedelta(days=4)
        residence_from = NOW + timedelta(hours=1)
        residence_to = NOW + timedelta(days=5)
        harness.confirm(
            _plan(
                label=f"interval-campuses-{terminal_campus}-{operation}",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": "location",
                        "source_key": "location:middle-campus",
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {"sheet_code": "location", "row_number": 30},
                        "content": {
                            "kind": "campus",
                            "business_code": "MIDDLE",
                            "display_name": "中间校区",
                            "display_path": "/中间校区",
                            "status": "active",
                        },
                    },
                    {
                        "operation": "upsert",
                        "fact_kind": "location",
                        "source_key": "location:final-campus",
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {"sheet_code": "location", "row_number": 31},
                        "content": {
                            "kind": "campus",
                            "business_code": "FINAL",
                            "display_name": "最终校区",
                            "display_path": "/最终校区",
                            "status": "active",
                        },
                    },
                ),
            ),
            label=f"interval-campuses-{terminal_campus}-{operation}",
        )
        if terminal_campus == "old":
            harness.confirm(
                _plan(
                    label=f"interval-old-{operation}",
                    base_version=harness.version,
                    changes=(
                        {
                            "operation": operation,
                            "fact_kind": "location",
                            "source_key": "location:campus",
                            "expected_version": 1,
                            "effective_at": NOW + timedelta(days=1),
                            "origin": {"sheet_code": "location", "row_number": 32},
                            "content": None,
                        },
                    ),
                ),
                label=f"interval-old-{operation}",
            )

        harness.confirm(
            _plan(
                label=f"interval-first-reparent-{terminal_campus}-{operation}",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": "location",
                        "source_key": "location:room-101",
                        "expected_version": 1,
                        "effective_at": second_at,
                        "origin": {"sheet_code": "location", "row_number": 33},
                        "content": {
                            "parent_source_key": "location:middle-campus",
                            "kind": "dorm_room",
                            "business_code": "A-101",
                            "display_name": "A栋101",
                            "display_path": "/中间校区/A栋101",
                            "status": "active",
                        },
                    },
                ),
            ),
            label=f"interval-first-reparent-{terminal_campus}-{operation}",
        )
        if terminal_campus == "middle":
            harness.confirm(
                _plan(
                    label=f"interval-middle-{operation}",
                    base_version=harness.version,
                    changes=(
                        {
                            "operation": operation,
                            "fact_kind": "location",
                            "source_key": "location:middle-campus",
                            "expected_version": 1,
                            "effective_at": NOW + timedelta(days=3),
                            "origin": {"sheet_code": "location", "row_number": 34},
                            "content": None,
                        },
                    ),
                ),
                label=f"interval-middle-{operation}",
            )

        harness.confirm(
            _plan(
                label=f"interval-second-reparent-{terminal_campus}-{operation}",
                base_version=harness.version,
                changes=(
                    {
                        "operation": "upsert",
                        "fact_kind": "location",
                        "source_key": "location:room-101",
                        "expected_version": 2,
                        "effective_at": third_at,
                        "origin": {"sheet_code": "location", "row_number": 35},
                        "content": {
                            "parent_source_key": "location:final-campus",
                            "kind": "dorm_room",
                            "business_code": "A-101",
                            "display_name": "A栋101",
                            "display_path": "/最终校区/A栋101",
                            "status": "active",
                        },
                    },
                ),
            ),
            label=f"interval-second-reparent-{terminal_campus}-{operation}",
        )
        with harness.factory() as session:  # type: ignore[operator]
            room_id = session.execute(
                text(
                    "SELECT room_location_id FROM dorm_beds "
                    "WHERE school_id = :school_id AND source_key = 'dorm-bed:101-1'"
                ),
                {"school_id": harness.school_id},
            ).scalar_one()
            assert (
                session.execute(
                    text(
                        "SELECT app.school_fact_location_history_interval_is_resolvable("
                        ":school_id, :room_id, :valid_from, :valid_to)"
                    ),
                    {
                        "school_id": harness.school_id,
                        "room_id": room_id,
                        "valid_from": residence_from,
                        "valid_to": residence_to,
                    },
                ).scalar_one()
                is False
            )

        source_key = f"residence:interval:{terminal_campus}:{operation}"
        with pytest.raises((SchoolFactChangeRejected, SchoolFactPersistenceError)):
            harness.confirm(
                _plan(
                    label=source_key,
                    base_version=harness.version,
                    changes=(
                        _residence_change(
                            operation="upsert",
                            source_key=source_key,
                            expected_version=0,
                            effective_at=residence_from,
                            row_number=36,
                            bed_source_key="dorm-bed:101-1",
                            valid_to=residence_to,
                        ),
                    ),
                ),
                label=source_key,
            )
        with harness.factory() as session:  # type: ignore[operator]
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM school_fact_change_lineage "
                        "WHERE school_id = :school_id "
                        "AND fact_kind = 'student_bed_residence' "
                        "AND fact_source_key = :source_key"
                    ),
                    {"school_id": harness.school_id, "source_key": source_key},
                ).scalar_one()
                == 0
            )


@pytest.mark.integration
@pytest.mark.parametrize("first_write", ("residence", "campus_inactive"))
def test_campus_inactive_and_open_residence_serialize_across_connections(
    isolated_migration_schema: PostgresRoles,
    first_write: str,
) -> None:
    """Two commits cannot write-skew into an inactive ancestry with an open residence."""

    roles = isolated_migration_schema
    with _residence_harness(roles, include_initial_residence=False) as harness:
        with harness.factory() as session:  # type: ignore[operator]
            campus_id, student_id, bed_id, source_id, database_now = session.execute(
                text(
                    "SELECT "
                    "(SELECT id FROM campus_locations WHERE school_id = :school_id "
                    " AND source_key = 'location:campus'), "
                    "(SELECT id FROM students WHERE school_id = :school_id "
                    " AND source_key = 'student:001'), "
                    "(SELECT id FROM dorm_beds WHERE school_id = :school_id "
                    " AND source_key = 'dorm-bed:101-1'), "
                    "(SELECT id FROM school_fact_sources WHERE school_id = :school_id "
                    " ORDER BY created_at, id LIMIT 1), statement_timestamp()"
                ),
                {"school_id": harness.school_id},
            ).one()
        residence_id = uuid4()

        def insert_residence(connection: psycopg.Connection[Any]) -> None:
            connection.execute(
                "INSERT INTO student_bed_residences "
                "(id, school_id, student_id, bed_id, valid_from, valid_to, "
                "revoked_at, version, source_id, import_batch_id) "
                "VALUES (%s, %s, %s, %s, %s, NULL, NULL, 1, %s, NULL)",
                (
                    residence_id,
                    harness.school_id,
                    student_id,
                    bed_id,
                    database_now - timedelta(hours=1),
                    source_id,
                ),
            )
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

        def stop_campus(connection: psycopg.Connection[Any]) -> None:
            connection.execute(
                "UPDATE campus_locations SET status = 'inactive', version = version + 1 "
                "WHERE school_id = %s AND id = %s",
                (harness.school_id, campus_id),
            )
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

        first_action, second_action = (
            (insert_residence, stop_campus)
            if first_write == "residence"
            else (stop_campus, insert_residence)
        )
        outcomes, serialization_wait = _run_competing_owner_transactions(
            roles,
            first_action=first_action,
            second_action=second_action,
            label=f"campus-residence-{first_write}",
        )

        assert serialization_wait == ("Lock", "advisory"), outcomes
        assert sorted(outcomes) == ["23514", "committed"]
        with harness.factory() as session:  # type: ignore[operator]
            campus_inactive, open_residence_count = session.execute(
                text(
                    "SELECT "
                    "(SELECT status = 'inactive' FROM campus_locations "
                    " WHERE school_id = :school_id AND id = :campus_id), "
                    "(SELECT count(*) FROM student_bed_residences "
                    " WHERE school_id = :school_id AND id = :residence_id "
                    " AND revoked_at IS NULL AND "
                    " (valid_to IS NULL OR valid_to > statement_timestamp()))"
                ),
                {
                    "school_id": harness.school_id,
                    "campus_id": campus_id,
                    "residence_id": residence_id,
                },
            ).one()
        assert not (campus_inactive and open_residence_count > 0)


@pytest.mark.integration
def test_database_anchor_rejects_second_school_root_before_cross_school_guard(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """A database is anchored to one school before any cross-school fixture can exist."""

    with _residence_harness(isolated_migration_schema) as school_a:
        assert school_a.school_id is not None
        with pytest.raises(
            IntegrityError,
            match="second school root violates the database anchor",
        ):
            with _residence_harness(isolated_migration_schema):
                pass


def test_campus_stop_cannot_commit_an_inactive_campus_with_effective_residence(
    current_master_data_schema: PostgresRoles,
) -> None:
    """Only the final invariant is frozen: reject or co-terminate are both acceptable."""

    with _residence_harness(current_master_data_schema) as harness:
        stopped_at = NOW + timedelta(days=1)
        plan = _plan(
            label="campus-stop-final-invariant",
            base_version=harness.version,
            changes=(
                _terminal_change(
                    "location",
                    "location:campus",
                    1,
                    stopped_at,
                    2,
                ),
            ),
        )
        try:
            _confirm(
                service=harness.service,
                change_set=plan,
                school_id=harness.school_id,
                user_id=harness.user_id,
                person_id=harness.person_id,
                assignment_id=harness.assignment_id,
                label="campus-stop-final-invariant",
            )
        except SchoolFactChangeRejected:
            pass
        except SchoolFactPersistenceError as exc:
            assert _is_integrity_rejection(exc), (
                "只有 PostgreSQL 完整性拒绝可作为校区停用门；不得吞掉其他持久化错误"
            )

        with harness.factory() as session:  # type: ignore[operator]
            campus_inactive = bool(
                session.execute(
                    text(
                        "SELECT status = 'inactive' FROM campus_locations "
                        "WHERE school_id = :school_id AND source_key = 'location:campus'"
                    ),
                    {"school_id": harness.school_id},
                ).scalar_one()
            )
            effective_residences_in_stopped_campus = int(
                session.execute(
                    text(
                        "WITH RECURSIVE stopped_locations(id) AS ("
                        "SELECT id FROM campus_locations "
                        "WHERE school_id = :school_id AND source_key = 'location:campus' "
                        "UNION ALL "
                        "SELECT child.id FROM campus_locations AS child "
                        "JOIN stopped_locations AS parent "
                        "ON child.parent_location_id = parent.id "
                        "WHERE child.school_id = :school_id) "
                        "SELECT count(*) FROM student_bed_residences AS residence "
                        "JOIN dorm_beds AS bed ON bed.school_id = residence.school_id "
                        "AND bed.id = residence.bed_id "
                        "WHERE residence.school_id = :school_id "
                        "AND bed.room_location_id IN (SELECT id FROM stopped_locations) "
                        "AND residence.valid_from <= :as_of "
                        "AND (residence.valid_to IS NULL OR residence.valid_to > :as_of) "
                        "AND residence.revoked_at IS NULL"
                    ),
                    {"school_id": harness.school_id, "as_of": stopped_at},
                ).scalar_one()
            )
        assert not (campus_inactive and effective_residences_in_stopped_campus > 0), (
            "正式确认留下了‘校区已停用 + 仍有有效入住’的禁止最终状态"
        )


def test_checkpoint2_registers_exactly_eight_finite_cross_layer_representatives() -> None:
    """Register eight independently collected nodes; this meta-test does not execute them."""

    representatives = {
        "root_lifecycle": (
            _root_lifecycle_test,
            ("_confirm(", "persistence._apply_change(", "_root_row("),
        ),
        "assignment_revoke": (
            _assignment_revoke_test,
            ("_confirm(", "SELECT valid_to, revoked_at"),
        ),
        "same_bed_residence": (
            _same_bed_test,
            ("preflight(", "harness.confirm(", "_assert_batch_cardinality", "_residence_row"),
        ),
        "a_to_b_to_c": (
            _a_to_b_to_c_test,
            ("first_report", "second_report", "harness.confirm(", "counts == (4, 4)"),
        ),
        "campus_inactive": (
            test_campus_stop_cannot_commit_an_inactive_campus_with_effective_residence,
            ("_confirm(", "WITH RECURSIVE stopped_locations", "student_bed_residences"),
        ),
        "organization_campus_confirmation": (
            _org_campus_confirm_test,
            ("_plan(", "harness.confirm(", "_formal_target_id", "organization_campus"),
        ),
        "student_campus_confirmation": (
            _student_campus_confirmation_test,
            ("_plan(", "harness.confirm(", "_formal_target_id", "administrative"),
        ),
        "policy_scope_confirmation": (
            _policy_scope_confirmation_test,
            ("_plan(", "harness.confirm(", "_formal_target_id", "target_field"),
        ),
    }
    assert tuple(representatives) == (
        "root_lifecycle",
        "assignment_revoke",
        "same_bed_residence",
        "a_to_b_to_c",
        "campus_inactive",
        "organization_campus_confirmation",
        "student_campus_confirmation",
        "policy_scope_confirmation",
    )
    assert len(representatives) == 8
    for test, required_source_tokens in representatives.values():
        expected_fixture = (
            "isolated_migration_schema"
            if test is _assignment_revoke_test
            else "current_master_data_schema"
        )
        assert expected_fixture in inspect.signature(test).parameters
        source = inspect.getsource(test)
        assert all(token in source for token in required_source_tokens)
