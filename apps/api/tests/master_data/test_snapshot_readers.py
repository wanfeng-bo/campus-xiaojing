from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_facts import HmacSchoolFactConfirmationCredentialIssuer
from app.db.uow import SqlAlchemyUnitOfWork
from app.domains.campus.repository import SqlAlchemyCampusRepository
from app.domains.students.repository import SqlAlchemyStudentRepository
from app.platform.calendar.contracts import WorkCalendarSnapshot
from app.platform.calendar.repository import SqlAlchemyWorkCalendarRepository
from app.platform.contracts import (
    AssignmentId,
    LocationId,
    OrganizationUnitId,
    PersonId,
    SchoolId,
    StudentId,
)
from app.platform.directory.repository import SqlAlchemyPersonRepository
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import compose_request_context
from app.platform.organization.repository import SqlAlchemyOrganizationRepository
from app.platform.school_facts.application import SchoolFactChangeService
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from app.platform.tenancy.repository import SqlAlchemySchoolRepository
from tests.master_data.conftest import PostgresRoles
from tests.master_data.test_execution_context import (
    ExecutionFacts,
    FakeRedis,
    _find_option,
    _resolver,
    execution_facts,
    runtime_session_factory,
)
from tests.school_facts.test_sf2_transaction import (
    CONFIRMATION_SIGNING_KEY,
    NOW,
    all_fact_kinds_change_set,
    context_for,
    make_owner_factory,
    sf2_environment,
)

_CONTEXT_KEY = b"master-data-schema-context-signing-key-32-bytes"

# 让 pytest 在本模块注册从 Task 6 上下文测试导入的共享真实数据库夹具。
assert execution_facts is not None
assert runtime_session_factory is not None


@pytest.fixture
def owner_historical_session_factory(
    current_master_data_schema: PostgresRoles,
) -> Iterator[sessionmaker[Session]]:
    """Use owner authority only to verify historical domain semantics in this module."""

    factory, engine = make_owner_factory(current_master_data_schema)
    try:
        yield factory
    finally:
        engine.dispose()  # type: ignore[union-attr]


def _constraint_name(error: object) -> str | None:
    diagnostic = getattr(error, "diag", None)
    return getattr(diagnostic, "constraint_name", None)


def _read_envelope(
    facts: ExecutionFacts,
    session_factory: sessionmaker[Session],
):
    resolver, identity_session, _token = _resolver(
        facts=facts,
        redis=FakeRedis(),
        runtime_session_factory=session_factory,
    )
    option = _find_option(resolver.list_options(), "staff_assignment")
    actor = resolver.resolve(option.opaque_ref)
    context = compose_request_context(actor, uuid4())
    envelope = ContextProofSigner(_CONTEXT_KEY).sign_execution_context(
        context,
        session_nonce_hash=identity_session.session_nonce_hash,
        proof_expires_at=option.effective_until,
    )
    return envelope


def _read_all_snapshots(
    facts: ExecutionFacts,
    session_factory: sessionmaker[Session],
    *,
    as_of: datetime,
):
    # Owner-only harness: this proves historical domain semantics, not a product Reader path.
    with session_factory() as session:
        people = SqlAlchemyPersonRepository(session)
        organizations = SqlAlchemyOrganizationRepository(session)
        students = SqlAlchemyStudentRepository(
            session,
            person_snapshot_resolver=people,
        )
        campus = SqlAlchemyCampusRepository(session)
        calendars = SqlAlchemyWorkCalendarRepository(session)
        return (
            people.resolve_person_snapshot(
                SchoolId(facts.school_id),
                PersonId(facts.student_person_id),
                as_of=as_of,
            ),
            organizations.resolve_assignment_snapshot(
                SchoolId(facts.school_id),
                AssignmentId(facts.assignment_id),
                as_of=as_of,
            ),
            students.resolve_student_relation_snapshot(
                SchoolId(facts.school_id),
                StudentId(facts.student_id),
                as_of=as_of,
            ),
            campus.resolve_location_snapshot(
                SchoolId(facts.school_id),
                LocationId(facts.location_id),
                as_of=as_of,
            ),
            calendars.get_published_snapshot(
                SchoolId(facts.school_id),
                as_of=as_of,
            ),
        )


@pytest.mark.integration
def test_historical_snapshots_do_not_change_after_rename(
    current_master_data_schema: PostgresRoles,
    owner_historical_session_factory: sessionmaker[Session],
    execution_facts: ExecutionFacts,
) -> None:
    occurred_at = execution_facts.valid_from + timedelta(days=1)
    captured = _read_all_snapshots(
        execution_facts,
        owner_historical_session_factory,
        as_of=occurred_at,
    )
    person_before, assignment_before, student_before, location_before, calendar_before = captured
    assert person_before is not None and person_before.display_name == "原姓名"
    assert assignment_before is not None
    assert student_before is not None and student_before.display_name == "原姓名"
    assert student_before.administrative_class_display_name == "初一（3）班"
    assert student_before.used_recorded_at_fallback is False
    assert location_before is not None and location_before.display_name == "旧校区名"
    assert isinstance(calendar_before, WorkCalendarSnapshot)
    assert calendar_before.display_name == "2026 学年校历"

    renamed_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=2)
    _publish_renames(current_master_data_schema, execution_facts, renamed_at)

    assert person_before.display_name == "原姓名"
    assert student_before.display_name == "原姓名"
    assert location_before.display_name == "旧校区名"

    old_snapshots = _read_all_snapshots(
        execution_facts,
        owner_historical_session_factory,
        as_of=occurred_at,
    )
    new_snapshots = _read_all_snapshots(
        execution_facts,
        owner_historical_session_factory,
        as_of=renamed_at,
    )
    assert old_snapshots[0] is not None and old_snapshots[0].display_name == "原姓名"
    assert old_snapshots[2] is not None and old_snapshots[2].display_name == "原姓名"
    assert old_snapshots[3] is not None and old_snapshots[3].display_name == "旧校区名"
    assert new_snapshots[0] is not None and new_snapshots[0].display_name == "新姓名"
    assert new_snapshots[2] is not None and new_snapshots[2].display_name == "新姓名"
    assert new_snapshots[3] is not None and new_snapshots[3].display_name == "新校区名"


@pytest.mark.integration
def test_historical_readers_do_not_join_current_identity_or_scope_tables(
    owner_historical_session_factory: sessionmaker[Session],
    execution_facts: ExecutionFacts,
) -> None:
    """真实执行 SQL 不得借当前账号、任职绑定或当前自然人状态冒充历史。"""

    engine = owner_historical_session_factory.kw["bind"]
    assert isinstance(engine, Engine)
    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with owner_historical_session_factory() as session:
            people = SqlAlchemyPersonRepository(session)
            organizations = SqlAlchemyOrganizationRepository(session)
            students = SqlAlchemyStudentRepository(
                session,
                person_snapshot_resolver=people,
            )
            campus = SqlAlchemyCampusRepository(session)
            as_of = execution_facts.valid_from + timedelta(days=1)
            assert (
                people.resolve_person_snapshot(
                    SchoolId(execution_facts.school_id),
                    PersonId(execution_facts.student_person_id),
                    as_of=as_of,
                )
                is not None
            )
            assert (
                organizations.resolve_assignment_snapshot(
                    SchoolId(execution_facts.school_id),
                    AssignmentId(execution_facts.assignment_id),
                    as_of=as_of,
                )
                is not None
            )
            assert (
                students.resolve_student_relation_snapshot(
                    SchoolId(execution_facts.school_id),
                    StudentId(execution_facts.student_id),
                    as_of=as_of,
                )
                is not None
            )
            assert (
                campus.resolve_location_snapshot(
                    SchoolId(execution_facts.school_id),
                    LocationId(execution_facts.location_id),
                    as_of=as_of,
                )
                is not None
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    historical_sql = "\n".join(
        statement
        for statement in statements
        if any(
            table in statement
            for table in (
                "school_person_names",
                "staff_assignments",
                "student_enrollments",
                "campus_location_versions",
            )
        )
        and "bind_execution_context" not in statement
    )
    assert " users " not in historical_sql
    assert "account_person_bindings" not in historical_sql
    assert "school_people" not in historical_sql
    assert "organization_units" not in historical_sql
    assert "administrative_classes" not in historical_sql
    assert "campus_locations" not in historical_sql


@pytest.mark.integration
@pytest.mark.parametrize("operation", ("end", "revoke"))
def test_person_historical_reader_applies_ledger_terminal_semantics(
    isolated_migration_schema: PostgresRoles,
    operation: str,
) -> None:
    """Root content uses the exact next lineage operation, not current root state."""

    owner_factory, owner_engine = make_owner_factory(isolated_migration_schema)
    try:
        with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
            school_id, user_id, person_id, assignment_id = facts
            service = SchoolFactChangeService(
                reader=persistence,
                writer=persistence,
                clock=lambda: NOW,
            )
            _confirm_school_fact_plan(
                service,
                all_fact_kinds_change_set(school_source_key=_school_source_key(factory, school_id)),
                facts,
                label=f"snapshot-person-seed-{operation}",
            )
            terminal_at = NOW + timedelta(days=2)
            terminal = _terminal_plan(
                base_version=2,
                fact_kind="person",
                source_key="person:parent",
                expected_version=1,
                operation=operation,
                effective_at=terminal_at,
            )
            _confirm_school_fact_plan(
                service,
                terminal,
                facts,
                label=f"snapshot-person-{operation}",
            )
            with factory() as owner_session:
                target_person_id = owner_session.execute(
                    text(
                        "SELECT id FROM school_people "
                        "WHERE school_id = :school_id AND source_key = 'person:parent'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()

            before_terminal = NOW + timedelta(days=1)
            with owner_factory() as owner_session:
                repository = SqlAlchemyPersonRepository(owner_session)
                before = repository.resolve_person_snapshot(
                    SchoolId(school_id),
                    PersonId(target_person_id),
                    as_of=before_terminal,
                )
                after = repository.resolve_person_snapshot(
                    SchoolId(school_id),
                    PersonId(target_person_id),
                    as_of=terminal_at,
                )
                bulk = repository.resolve_person_snapshots(
                    SchoolId(school_id),
                    (PersonId(target_person_id),),
                    as_of=before_terminal,
                )
    finally:
        owner_engine.dispose()  # type: ignore[union-attr]

    if operation == "end":
        assert before is not None
        assert PersonId(target_person_id) in bulk
    else:
        assert before is None
        assert PersonId(target_person_id) not in bulk
    assert after is None


@pytest.mark.integration
@pytest.mark.parametrize("fact_kind", ("organization", "assignment"))
def test_assignment_historical_reader_applies_ledger_revoke_semantics(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
) -> None:
    """Assignment and its organization content both honor revoke lineage."""

    owner_factory, owner_engine = make_owner_factory(isolated_migration_schema)
    try:
        with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
            school_id, _user_id, _person_id, _assignment_id = facts
            service = SchoolFactChangeService(
                reader=persistence,
                writer=persistence,
                clock=lambda: NOW,
            )
            _confirm_school_fact_plan(
                service,
                all_fact_kinds_change_set(school_source_key=_school_source_key(factory, school_id)),
                facts,
                label=f"snapshot-assignment-seed-{fact_kind}",
            )
            source_key = "organization:dorm" if fact_kind == "organization" else "assignment:parent"
            _confirm_school_fact_plan(
                service,
                _terminal_plan(
                    base_version=2,
                    fact_kind=fact_kind,
                    source_key=source_key,
                    expected_version=1,
                    operation="revoke",
                    effective_at=NOW + timedelta(days=2),
                ),
                facts,
                label=f"snapshot-assignment-{fact_kind}-revoke",
            )
            with factory() as owner_session:
                target_assignment_id = owner_session.execute(
                    text(
                        "SELECT id FROM staff_assignments "
                        "WHERE school_id = :school_id "
                        "AND source_key = 'assignment:parent'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                if fact_kind == "assignment":
                    # Keep the physical row apparently readable: the ledger must be the
                    # mechanism that suppresses a revoked historical assignment.
                    owner_session.execute(
                        text(
                            "UPDATE staff_assignments "
                            "SET status = 'confirmed', valid_to = NULL WHERE id = :id"
                        ),
                        {"id": target_assignment_id},
                    )
                    owner_session.commit()

            with owner_factory() as owner_session:
                snapshot = SqlAlchemyOrganizationRepository(
                    owner_session
                ).resolve_assignment_snapshot(
                    SchoolId(school_id),
                    AssignmentId(target_assignment_id),
                    as_of=NOW + timedelta(days=1),
                )
    finally:
        owner_engine.dispose()  # type: ignore[union-attr]
    assert snapshot is None


@pytest.mark.integration
def test_current_organization_and_student_readers_use_future_root_end_boundary(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Current readers use formal root time, not the latest-declared stable status."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    terminal_at = checked_now + timedelta(days=2)
    owner_factory, owner_engine = make_owner_factory(isolated_migration_schema)
    try:
        with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
            school_id, _user_id, _person_id, _assignment_id = facts
            service = SchoolFactChangeService(
                reader=persistence,
                writer=persistence,
                clock=lambda: checked_now,
            )
            _confirm_school_fact_plan(
                service,
                all_fact_kinds_change_set(school_source_key=_school_source_key(factory, school_id)),
                facts,
                label="snapshot-current-root-future-end-seed",
            )
            class_id = uuid4()
            marker = uuid4().hex
            with factory.begin() as owner_session:
                student_id = owner_session.execute(
                    text(
                        "SELECT id FROM students WHERE school_id = :school_id "
                        "AND source_key = 'student:001'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                assignment_id = owner_session.execute(
                    text(
                        "SELECT id FROM staff_assignments WHERE school_id = :school_id "
                        "AND source_key = 'assignment:parent'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                owner_session.execute(
                    text(
                        "INSERT INTO administrative_classes "
                        "(id, school_id, school_year_code, grade_code, status, version) "
                        "VALUES (:id, :school_id, '2026', 'grade_1', 'active', 1)"
                    ),
                    {"id": class_id, "school_id": school_id},
                )
                owner_session.execute(
                    text(
                        "INSERT INTO administrative_class_versions "
                        "(id, school_id, administrative_class_id, display_name, valid_from, "
                        "source_kind, source_ref, status, version) VALUES "
                        "(:id, :school_id, :class_id, '一年级一班', :valid_from, "
                        "'manual', :source_ref, 'confirmed', 1)"
                    ),
                    {
                        "id": uuid4(),
                        "school_id": school_id,
                        "class_id": class_id,
                        "valid_from": NOW,
                        "source_ref": marker,
                    },
                )
                owner_session.execute(
                    text(
                        "INSERT INTO student_enrollments "
                        "(id, school_id, student_id, administrative_class_id, student_number, "
                        "source_kind, source_ref, valid_from, status, version) VALUES "
                        "(:id, :school_id, :student_id, :class_id, 'S-001', "
                        "'manual', :source_ref, :valid_from, 'confirmed', 1)"
                    ),
                    {
                        "id": uuid4(),
                        "school_id": school_id,
                        "student_id": student_id,
                        "class_id": class_id,
                        "source_ref": marker,
                        "valid_from": NOW,
                    },
                )

            terminal = CanonicalFactChangeSetV1.model_validate(
                {
                    "source_sha256": sha256(b"snapshot-current-root-future-end").hexdigest(),
                    "base_version": 2,
                    "changes": (
                        {
                            "operation": "end",
                            "fact_kind": "organization",
                            "source_key": "organization:dorm",
                            "expected_version": 1,
                            "effective_at": terminal_at,
                            "origin": {"sheet_code": "snapshot_reader", "row_number": 1},
                            "content": None,
                        },
                        {
                            "operation": "end",
                            "fact_kind": "student",
                            "source_key": "student:001",
                            "expected_version": 1,
                            "effective_at": terminal_at,
                            "origin": {"sheet_code": "snapshot_reader", "row_number": 2},
                            "content": None,
                        },
                    ),
                }
            )
            _confirm_school_fact_plan(
                service,
                terminal,
                facts,
                label="snapshot-current-root-future-end",
            )

            with owner_factory() as owner_session:
                organizations = SqlAlchemyOrganizationRepository(owner_session)
                people = SqlAlchemyPersonRepository(owner_session)
                students = SqlAlchemyStudentRepository(
                    owner_session,
                    person_snapshot_resolver=people,
                )
                assignment_before = organizations.get_current_assignment(
                    SchoolId(school_id),
                    AssignmentId(assignment_id),
                    as_of=terminal_at - timedelta(seconds=1),
                )
                assignment_after = organizations.get_current_assignment(
                    SchoolId(school_id),
                    AssignmentId(assignment_id),
                    as_of=terminal_at,
                )
                student_before = students.get_current_student_relation(
                    SchoolId(school_id),
                    StudentId(student_id),
                    as_of=terminal_at - timedelta(seconds=1),
                )
                student_after = students.get_current_student_relation(
                    SchoolId(school_id),
                    StudentId(student_id),
                    as_of=terminal_at,
                )
    finally:
        owner_engine.dispose()  # type: ignore[union-attr]

    assert assignment_before is not None
    assert assignment_after is None
    assert student_before is not None
    assert student_after is None


@pytest.mark.integration
@pytest.mark.parametrize("operation", ("end", "revoke"))
def test_location_historical_reader_applies_ancestor_terminal_semantics(
    isolated_migration_schema: PostgresRoles,
    operation: str,
) -> None:
    """A child snapshot follows every ancestor's ledger boundary, not current status."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    owner_factory, owner_engine = make_owner_factory(isolated_migration_schema)
    try:
        with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
            school_id, _user_id, _person_id, _assignment_id = facts
            service = SchoolFactChangeService(
                reader=persistence,
                writer=persistence,
                clock=lambda: checked_now,
            )
            seed_payload = all_fact_kinds_change_set(
                school_source_key=_school_source_key(factory, school_id)
            ).model_dump(mode="python", exclude_none=True)
            seed_payload["changes"] = tuple(
                change
                for change in seed_payload["changes"]
                if change["fact_kind"] != "student_bed_residence"
            )
            _confirm_school_fact_plan(
                service,
                CanonicalFactChangeSetV1.model_validate(seed_payload),
                facts,
                label=f"snapshot-location-ancestor-seed-{operation}",
            )
            with factory() as owner_session:
                room_id = owner_session.execute(
                    text(
                        "SELECT id FROM campus_locations "
                        "WHERE school_id = :school_id "
                        "AND source_key = 'location:room-101'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()

            terminal_at = checked_now
            _confirm_school_fact_plan(
                service,
                _terminal_plan(
                    base_version=2,
                    fact_kind="location",
                    source_key="location:campus",
                    expected_version=1,
                    operation=operation,
                    effective_at=terminal_at,
                ),
                facts,
                label=f"snapshot-location-ancestor-{operation}",
            )
            if operation == "revoke":
                reactivation = CanonicalFactChangeSetV1.model_validate(
                    {
                        "source_sha256": sha256(
                            b"snapshot-location-ancestor-reactivate"
                        ).hexdigest(),
                        "base_version": 3,
                        "changes": (
                            {
                                "operation": "reactivate",
                                "fact_kind": "location",
                                "source_key": "location:campus",
                                "expected_version": 2,
                                "effective_at": checked_now + timedelta(seconds=2),
                                "origin": {
                                    "sheet_code": "snapshot_reader",
                                    "row_number": 2,
                                },
                                "content": {
                                    "kind": "campus",
                                    "business_code": "MAIN",
                                    "display_name": "恢复的主校区",
                                    "display_path": "/恢复的主校区",
                                    "status": "active",
                                },
                            },
                        ),
                    }
                )
                _confirm_school_fact_plan(
                    service,
                    reactivation,
                    facts,
                    label="snapshot-location-ancestor-reactivate",
                )

            with owner_factory() as owner_session:
                repository = SqlAlchemyCampusRepository(owner_session)
                before = repository.resolve_location_snapshot(
                    SchoolId(school_id),
                    LocationId(room_id),
                    as_of=terminal_at - timedelta(seconds=1),
                )
                after = repository.resolve_location_snapshot(
                    SchoolId(school_id),
                    LocationId(room_id),
                    as_of=terminal_at + timedelta(seconds=1),
                )
                after_reactivation = (
                    repository.resolve_location_snapshot(
                        SchoolId(school_id),
                        LocationId(room_id),
                        as_of=checked_now + timedelta(seconds=3),
                    )
                    if operation == "revoke"
                    else None
                )
    finally:
        owner_engine.dispose()  # type: ignore[union-attr]

    if operation == "end":
        assert before is not None
    else:
        assert before is None
        assert after_reactivation is not None
    assert after is None


@pytest.mark.integration
def test_location_reparent_and_kind_change_preserve_exact_structure_history(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Formal structural changes keep one stable identity and exact as-of slices."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    owner_factory, owner_engine = make_owner_factory(isolated_migration_schema)
    try:
        with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
            school_id, _user_id, _person_id, _assignment_id = facts
            service = SchoolFactChangeService(
                reader=persistence,
                writer=persistence,
                clock=lambda: checked_now,
            )
            seed_payload = all_fact_kinds_change_set(
                school_source_key=_school_source_key(factory, school_id)
            ).model_dump(mode="python", exclude_none=True)
            seed_payload["changes"] = tuple(
                change
                for change in seed_payload["changes"]
                if change["fact_kind"] != "student_bed_residence"
            ) + (
                {
                    "operation": "upsert",
                    "fact_kind": "location",
                    "source_key": "location:campus-2",
                    "expected_version": 0,
                    "effective_at": NOW,
                    "origin": {"sheet_code": "location", "row_number": 90},
                    "content": {
                        "kind": "campus",
                        "business_code": "SECOND",
                        "display_name": "第二校区",
                        "display_path": "/第二校区",
                        "status": "active",
                    },
                },
            )
            _confirm_school_fact_plan(
                service,
                CanonicalFactChangeSetV1.model_validate(seed_payload),
                facts,
                label="snapshot-location-reparent-seed",
            )
            with factory() as owner_session:
                room_id, old_parent_id, new_parent_id, source_key = owner_session.execute(
                    text(
                        "SELECT room.id, room.parent_location_id, campus.id, room.source_key "
                        "FROM campus_locations AS room "
                        "JOIN campus_locations AS campus ON campus.school_id = room.school_id "
                        "AND campus.source_key = 'location:campus-2' "
                        "WHERE room.school_id = :school_id "
                        "AND room.source_key = 'location:room-101'"
                    ),
                    {"school_id": school_id},
                ).one()

            with owner_factory() as owner_session:
                before = SqlAlchemyCampusRepository(owner_session).resolve_location_snapshot(
                    SchoolId(school_id),
                    LocationId(room_id),
                    as_of=checked_now - timedelta(seconds=1),
                )
            assert before is not None

            reparent = CanonicalFactChangeSetV1.model_validate(
                {
                    "source_sha256": sha256(b"snapshot-location-reparent").hexdigest(),
                    "base_version": 2,
                    "changes": (
                        {
                            "operation": "upsert",
                            "fact_kind": "location",
                            "source_key": "location:room-101",
                            "expected_version": 1,
                            "effective_at": checked_now + timedelta(seconds=1),
                            "origin": {"sheet_code": "snapshot_reader", "row_number": 3},
                            "content": {
                                "parent_source_key": "location:campus-2",
                                "kind": "dorm_building",
                                "business_code": "A-101",
                                "display_name": "改挂后的A栋",
                                "display_path": "/第二校区/A栋",
                                "status": "active",
                            },
                        },
                    ),
                }
            )
            reparent_result = _confirm_school_fact_plan(
                service,
                reparent,
                facts,
                label="snapshot-location-reparent",
            )
            with owner_factory() as owner_session:
                repository = SqlAlchemyCampusRepository(owner_session)
                current_before_effective_at = repository.get_current_location(
                    SchoolId(school_id),
                    LocationId(room_id),
                    as_of=checked_now,
                )
                before = repository.resolve_location_snapshot(
                    SchoolId(school_id),
                    LocationId(room_id),
                    as_of=checked_now - timedelta(seconds=1),
                )
                after = repository.resolve_location_snapshot(
                    SchoolId(school_id),
                    LocationId(room_id),
                    as_of=checked_now + timedelta(seconds=2),
                )
            with factory() as owner_session:
                stable = owner_session.execute(
                    text(
                        "SELECT id, source_key, parent_location_id, kind, version "
                        "FROM campus_locations WHERE school_id = :school_id AND id = :id"
                    ),
                    {"school_id": school_id, "id": room_id},
                ).one()
                slices = owner_session.execute(
                    text(
                        "SELECT version, parent_location_id, kind "
                        "FROM campus_location_versions "
                        "WHERE school_id = :school_id AND location_id = :id "
                        "ORDER BY version"
                    ),
                    {"school_id": school_id, "id": room_id},
                ).all()
                lineage = owner_session.execute(
                    text(
                        "SELECT lineage.target_version, lineage.unit_id, lineage.batch_id "
                        "FROM school_fact_change_lineage AS lineage "
                        "JOIN master_data_import_units AS unit "
                        "ON unit.school_id = lineage.school_id AND unit.id = lineage.unit_id "
                        "AND unit.batch_id = lineage.batch_id "
                        "WHERE lineage.school_id = :school_id "
                        "AND lineage.fact_kind = 'location' AND lineage.target_id = :id "
                        "AND lineage.operation = 'upsert' AND unit.status = 'applied' "
                        "ORDER BY lineage.target_version"
                    ),
                    {"school_id": school_id, "id": room_id},
                ).all()
                audit_count = int(
                    owner_session.execute(
                        text(
                            "SELECT count(*) FROM audit_logs "
                            "WHERE school_id = :school_id "
                            "AND target_type = 'master_data_import_batch' "
                            "AND target_id = :batch_id"
                        ),
                        {
                            "school_id": school_id,
                            "batch_id": str(reparent_result.batch_id),
                        },
                    ).scalar_one()
                )
                with pytest.raises(IntegrityError) as exc_info, owner_session.begin_nested():
                    owner_session.execute(
                        text(
                            "UPDATE campus_locations "
                            "SET parent_location_id = :parent_id, kind = 'dorm_room', "
                            "version = 1 WHERE school_id = :school_id AND id = :id"
                        ),
                        {
                            "school_id": school_id,
                            "id": room_id,
                            "parent_id": old_parent_id,
                        },
                    )
                    owner_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                assert getattr(exc_info.value.orig, "sqlstate", None) == "23514"
                assert (
                    _constraint_name(exc_info.value.orig) == "ck_campus_location_structure_lineage"
                )
    finally:
        owner_engine.dispose()  # type: ignore[union-attr]

    assert before is not None
    assert before.location_id == LocationId(room_id)
    assert before.parent_location_id == LocationId(old_parent_id)
    assert before.kind == "dorm_room"
    assert current_before_effective_at is not None
    assert current_before_effective_at.parent_location_id == LocationId(old_parent_id)
    assert current_before_effective_at.kind == "dorm_room"
    assert after is not None
    assert after.location_id == LocationId(room_id)
    assert after.parent_location_id == LocationId(new_parent_id)
    assert after.kind == "dorm_building"
    assert stable == (room_id, source_key, new_parent_id, "dorm_building", 2)
    assert slices == [
        (1, old_parent_id, "dorm_room"),
        (2, new_parent_id, "dorm_building"),
    ]
    assert [row.target_version for row in lineage] == [1, 2]
    assert len({row.unit_id for row in lineage}) == 2
    assert len({row.batch_id for row in lineage}) == 2
    assert audit_count == 1


@pytest.mark.integration
def test_organization_reparent_preserves_exact_parent_history(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Organization reparenting versions the parent without replacing its identity."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    owner_factory, owner_engine = make_owner_factory(isolated_migration_schema)
    try:
        with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
            school_id, _user_id, _person_id, _assignment_id = facts
            service = SchoolFactChangeService(
                reader=persistence,
                writer=persistence,
                clock=lambda: checked_now,
            )
            seed_payload = all_fact_kinds_change_set(
                school_source_key=_school_source_key(factory, school_id)
            ).model_dump(mode="python", exclude_none=True)
            seed_payload["changes"] = tuple(seed_payload["changes"]) + (
                {
                    "operation": "upsert",
                    "fact_kind": "organization",
                    "source_key": "organization:parent-a",
                    "expected_version": 0,
                    "effective_at": NOW,
                    "origin": {"sheet_code": "organization", "row_number": 90},
                    "content": {
                        "kind": "schoolwide",
                        "business_code": "PARENT-A",
                        "display_name": "原上级",
                        "display_path": "/原上级",
                        "status": "active",
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "organization",
                    "source_key": "organization:parent-b",
                    "expected_version": 0,
                    "effective_at": NOW,
                    "origin": {"sheet_code": "organization", "row_number": 91},
                    "content": {
                        "kind": "schoolwide",
                        "business_code": "PARENT-B",
                        "display_name": "新上级",
                        "display_path": "/新上级",
                        "status": "active",
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "organization",
                    "source_key": "organization:reparented",
                    "expected_version": 0,
                    "effective_at": NOW,
                    "origin": {"sheet_code": "organization", "row_number": 92},
                    "content": {
                        "parent_source_key": "organization:parent-a",
                        "kind": "department",
                        "business_code": "CHILD",
                        "display_name": "待改挂组织",
                        "display_path": "/原上级/待改挂组织",
                        "status": "active",
                    },
                },
            )
            _confirm_school_fact_plan(
                service,
                CanonicalFactChangeSetV1.model_validate(seed_payload),
                facts,
                label="snapshot-organization-reparent-seed",
            )
            with factory() as owner_session:
                child_id, old_parent_id, new_parent_id, source_key = owner_session.execute(
                    text(
                        "SELECT child.id, child.parent_organization_unit_id, parent.id, "
                        "child.source_key FROM organization_units AS child "
                        "JOIN organization_units AS parent ON parent.school_id = child.school_id "
                        "AND parent.source_key = 'organization:parent-b' "
                        "WHERE child.school_id = :school_id "
                        "AND child.source_key = 'organization:reparented'"
                    ),
                    {"school_id": school_id},
                ).one()

            reparent = CanonicalFactChangeSetV1.model_validate(
                {
                    "source_sha256": sha256(b"snapshot-organization-reparent").hexdigest(),
                    "base_version": 2,
                    "changes": (
                        {
                            "operation": "upsert",
                            "fact_kind": "organization",
                            "source_key": "organization:reparented",
                            "expected_version": 1,
                            "effective_at": checked_now + timedelta(seconds=1),
                            "origin": {"sheet_code": "snapshot_reader", "row_number": 5},
                            "content": {
                                "parent_source_key": "organization:parent-b",
                                "kind": "department",
                                "business_code": "CHILD",
                                "display_name": "改挂后组织",
                                "display_path": "/新上级/改挂后组织",
                                "status": "active",
                            },
                        },
                    ),
                }
            )
            reparent_result = _confirm_school_fact_plan(
                service,
                reparent,
                facts,
                label="snapshot-organization-reparent",
            )

            with owner_factory() as owner_session:
                repository = SqlAlchemyOrganizationRepository(owner_session)
                before = repository.resolve_organization_unit_snapshot(
                    SchoolId(school_id),
                    OrganizationUnitId(child_id),
                    as_of=checked_now - timedelta(seconds=1),
                )
                after = repository.resolve_organization_unit_snapshot(
                    SchoolId(school_id),
                    OrganizationUnitId(child_id),
                    as_of=checked_now + timedelta(seconds=2),
                )
            with factory() as owner_session:
                stable = owner_session.execute(
                    text(
                        "SELECT id, source_key, parent_organization_unit_id, version "
                        "FROM organization_units WHERE school_id = :school_id AND id = :id"
                    ),
                    {"school_id": school_id, "id": child_id},
                ).one()
                slices = owner_session.execute(
                    text(
                        "SELECT version, parent_organization_unit_id "
                        "FROM organization_unit_versions "
                        "WHERE school_id = :school_id AND organization_unit_id = :id "
                        "ORDER BY version"
                    ),
                    {"school_id": school_id, "id": child_id},
                ).all()
                lineage = owner_session.execute(
                    text(
                        "SELECT target_version, unit_id, batch_id "
                        "FROM school_fact_change_lineage WHERE school_id = :school_id "
                        "AND fact_kind = 'organization' AND target_id = :id "
                        "AND operation = 'upsert' ORDER BY target_version"
                    ),
                    {"school_id": school_id, "id": child_id},
                ).all()
                audit_count = int(
                    owner_session.execute(
                        text(
                            "SELECT count(*) FROM audit_logs "
                            "WHERE school_id = :school_id "
                            "AND target_type = 'master_data_import_batch' "
                            "AND target_id = :batch_id"
                        ),
                        {
                            "school_id": school_id,
                            "batch_id": str(reparent_result.batch_id),
                        },
                    ).scalar_one()
                )
                with pytest.raises(IntegrityError) as exc_info, owner_session.begin_nested():
                    owner_session.execute(
                        text(
                            "UPDATE organization_units "
                            "SET parent_organization_unit_id = :parent_id, version = 1 "
                            "WHERE school_id = :school_id AND id = :id"
                        ),
                        {
                            "school_id": school_id,
                            "id": child_id,
                            "parent_id": old_parent_id,
                        },
                    )
                    owner_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                assert getattr(exc_info.value.orig, "sqlstate", None) == "23514"
                assert (
                    _constraint_name(exc_info.value.orig)
                    == "ck_organization_unit_structure_lineage"
                )
    finally:
        owner_engine.dispose()  # type: ignore[union-attr]

    assert before is not None
    assert before.organization_unit_id == OrganizationUnitId(child_id)
    assert before.parent_organization_unit_id == OrganizationUnitId(old_parent_id)
    assert after is not None
    assert after.organization_unit_id == OrganizationUnitId(child_id)
    assert after.parent_organization_unit_id == OrganizationUnitId(new_parent_id)
    assert stable == (child_id, source_key, new_parent_id, 2)
    assert slices == [(1, old_parent_id), (2, new_parent_id)]
    assert [row.target_version for row in lineage] == [1, 2]
    assert len({row.unit_id for row in lineage}) == 2
    assert len({row.batch_id for row in lineage}) == 2
    assert audit_count == 1


@pytest.mark.integration
@pytest.mark.parametrize("connection_role", ("runtime", "owner"))
@pytest.mark.parametrize(
    ("fact_kind", "mutation"),
    (
        ("location", "update"),
        ("location", "insert"),
        ("organization", "update"),
        ("organization", "insert"),
    ),
)
def test_runtime_cannot_mutate_structure_versions_without_formal_lineage(
    isolated_migration_schema: PostgresRoles,
    connection_role: str,
    fact_kind: str,
    mutation: str,
) -> None:
    """Version-slice structure is writable only through a formal applied unit."""

    runtime_factory, runtime_engine = _runtime_factory(isolated_migration_schema)
    try:
        with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
            school_id, _user_id, _person_id, _assignment_id = facts
            service = SchoolFactChangeService(
                reader=persistence,
                writer=persistence,
                clock=lambda: datetime.now(UTC),
            )
            _confirm_school_fact_plan(
                service,
                all_fact_kinds_change_set(school_source_key=_school_source_key(factory, school_id)),
                facts,
                label=f"structure-direct-{connection_role}-{fact_kind}-{mutation}",
            )
            envelope = _sf2_read_envelope(
                facts,
                label=f"structure-direct-{connection_role}-{fact_kind}-{mutation}",
            )
            expected_constraint = (
                "ck_campus_location_version_structure_lineage"
                if fact_kind == "location"
                else "ck_organization_unit_version_structure_lineage"
            )

            def mutate_structure(session: Session) -> None:
                if fact_kind == "location" and mutation == "update":
                    session.execute(
                        text(
                            "UPDATE campus_location_versions "
                            "SET kind = 'dorm_building' "
                            "WHERE school_id = :school_id "
                            "AND location_id = ("
                            "SELECT id FROM campus_locations "
                            "WHERE school_id = :school_id "
                            "AND source_key = 'location:room-101') "
                            "AND valid_to IS NULL"
                        ),
                        {"school_id": school_id},
                    )
                elif fact_kind == "organization" and mutation == "update":
                    session.execute(
                        text(
                            "UPDATE organization_unit_versions "
                            "SET valid_from = valid_from - interval '1 minute' "
                            "WHERE school_id = :school_id "
                            "AND organization_unit_id = ("
                            "SELECT id FROM organization_units "
                            "WHERE school_id = :school_id "
                            "AND source_key = 'organization:dorm') "
                            "AND valid_to IS NULL"
                        ),
                        {"school_id": school_id},
                    )
                elif fact_kind == "location":
                    session.execute(
                        text(
                            "INSERT INTO campus_location_versions ("
                            "id, school_id, location_id, parent_location_id, kind, "
                            "business_code, display_name, aliases, display_path, "
                            "valid_from, valid_to, source_kind, source_ref, status, version"
                            ") SELECT :id, version.school_id, version.location_id, "
                            "version.parent_location_id, version.kind, version.business_code, "
                            "version.display_name, version.aliases, version.display_path, "
                            "version.valid_from + interval '30 days', "
                            "version.valid_from + interval '31 days', 'manual', :source_ref, "
                            "'draft', 99 FROM campus_location_versions AS version "
                            "JOIN campus_locations AS location "
                            "ON location.school_id = version.school_id "
                            "AND location.id = version.location_id "
                            "WHERE version.school_id = :school_id "
                            "AND location.source_key = 'location:room-101' "
                            "AND version.valid_to IS NULL"
                        ),
                        {
                            "id": uuid4(),
                            "school_id": school_id,
                            "source_ref": f"runtime-direct:{uuid4().hex}",
                        },
                    )
                else:
                    session.execute(
                        text(
                            "INSERT INTO organization_unit_versions ("
                            "id, school_id, organization_unit_id, "
                            "parent_organization_unit_id, business_code, display_name, "
                            "short_name, display_path, valid_from, valid_to, source_kind, "
                            "source_ref, status, version"
                            ") SELECT :id, version.school_id, version.organization_unit_id, "
                            "version.parent_organization_unit_id, version.business_code, "
                            "version.display_name, version.short_name, version.display_path, "
                            "version.valid_from + interval '30 days', "
                            "version.valid_from + interval '31 days', 'manual', :source_ref, "
                            "'draft', 99 FROM organization_unit_versions AS version "
                            "JOIN organization_units AS organization "
                            "ON organization.school_id = version.school_id "
                            "AND organization.id = version.organization_unit_id "
                            "WHERE version.school_id = :school_id "
                            "AND organization.source_key = 'organization:dorm' "
                            "AND version.valid_to IS NULL"
                        ),
                        {
                            "id": uuid4(),
                            "school_id": school_id,
                            "source_ref": f"runtime-direct:{uuid4().hex}",
                        },
                    )
                session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

            if connection_role == "runtime":
                with pytest.raises(DBAPIError) as exc_info:
                    with SqlAlchemyUnitOfWork(runtime_factory, envelope) as uow:
                        mutate_structure(uow.session)
                assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"
            else:
                with pytest.raises(IntegrityError) as exc_info:
                    with factory() as owner_session, owner_session.begin():
                        mutate_structure(owner_session)
                assert getattr(exc_info.value.orig, "sqlstate", None) == "23514"
                assert _constraint_name(exc_info.value.orig) == expected_constraint
    finally:
        runtime_engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("write_branch", ("insert", "close_previous"))
@pytest.mark.parametrize(
    "mismatch",
    (
        "lineage_source",
        "payload_source",
        "payload_operation",
        "payload_effective_at",
        "payload_expected_version",
        "unit_shape",
    ),
)
def test_structure_version_rejects_mismatched_applied_ledger_chain(
    isolated_migration_schema: PostgresRoles,
    write_branch: str,
    mismatch: str,
) -> None:
    """A coincidental target/version match is not a formal structural action."""

    runtime_factory, runtime_engine = _runtime_factory(isolated_migration_schema)
    try:
        with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
            school_id, _user_id, _person_id, _assignment_id = facts
            service = SchoolFactChangeService(
                reader=persistence,
                writer=persistence,
                clock=lambda: datetime.now(UTC),
            )
            _confirm_school_fact_plan(
                service,
                all_fact_kinds_change_set(school_source_key=_school_source_key(factory, school_id)),
                facts,
                label=f"structure-ledger-mismatch-{write_branch}-{mismatch}",
            )
            effective_at = NOW + timedelta(days=1)
            with factory() as owner_session:
                campus_id = owner_session.execute(
                    text(
                        "SELECT id FROM campus_locations "
                        "WHERE school_id = :school_id "
                        "AND source_key = 'location:campus'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                original = owner_session.execute(
                    text(
                        "SELECT stable.id, stable.source_key, lineage.id AS lineage_id, "
                        "lineage.batch_id, lineage.unit_id "
                        "FROM campus_locations AS stable "
                        "JOIN school_fact_change_lineage AS lineage "
                        "ON lineage.school_id = stable.school_id "
                        "AND lineage.fact_kind = 'location' "
                        "AND lineage.fact_source_key = stable.source_key "
                        "AND lineage.target_id = stable.id "
                        "AND lineage.target_version = 1 "
                        "WHERE stable.school_id = :school_id "
                        "AND stable.source_key = 'location:room-101'"
                    ),
                    {"school_id": school_id},
                ).one()

            if write_branch == "insert":
                target_id = uuid4()
                target_source_key = f"location:forged:{uuid4().hex}"
                target_version = 1
                expected_version = 0
                target_kind = "building"
            else:
                target_id = original.id
                target_source_key = str(original.source_key)
                target_version = 2
                expected_version = 1
                target_kind = "dorm_room"

            lineage_source_key = (
                f"location:wrong-lineage:{uuid4().hex}"
                if mismatch == "lineage_source"
                else target_source_key
            )
            payload_source_key = (
                f"location:wrong-payload:{uuid4().hex}"
                if mismatch == "payload_source"
                else target_source_key
            )
            payload_operation = "end" if mismatch == "payload_operation" else "upsert"
            payload_fact_kind = "person" if mismatch == "unit_shape" else "location"
            unit_kind = "person" if mismatch == "unit_shape" else "school_fact"
            payload_effective_at = (
                effective_at + timedelta(hours=1)
                if mismatch == "payload_effective_at"
                else effective_at
            )
            payload_expected_version = (
                expected_version + 7 if mismatch == "payload_expected_version" else expected_version
            )

            with pytest.raises(IntegrityError) as exc_info:
                with factory() as owner_session, owner_session.begin():
                    if write_branch == "insert":
                        owner_session.execute(
                            text(
                                "INSERT INTO campus_locations ("
                                "id, school_id, source_key, parent_location_id, kind, "
                                "status, version) VALUES ("
                                ":id, :school_id, :source_key, :parent_id, :target_kind, "
                                "'active', 1)"
                            ),
                            {
                                "id": target_id,
                                "school_id": school_id,
                                "source_key": target_source_key,
                                "parent_id": campus_id,
                                "target_kind": target_kind,
                            },
                        )
                    else:
                        owner_session.execute(
                            text(
                                "UPDATE campus_locations SET version = 2 "
                                "WHERE school_id = :school_id AND id = :id"
                            ),
                            {"school_id": school_id, "id": target_id},
                        )

                    owner_session.execute(
                        text(
                            "ALTER TABLE school_fact_change_lineage DISABLE TRIGGER "
                            "trg_sf2_reject_lineage_update"
                        )
                    )
                    owner_session.execute(
                        text("ALTER TABLE school_fact_change_lineage DISABLE ROW LEVEL SECURITY")
                    )
                    lineage_update = owner_session.execute(
                        text(
                            "UPDATE school_fact_change_lineage SET "
                            "fact_source_key = :lineage_source_key, operation = 'upsert', "
                            "target_id = :target_id, target_version = :target_version "
                            "WHERE school_id = :school_id AND id = :lineage_id"
                        ),
                        {
                            "school_id": school_id,
                            "lineage_id": original.lineage_id,
                            "lineage_source_key": lineage_source_key,
                            "target_id": target_id,
                            "target_version": target_version,
                        },
                    )
                    assert getattr(lineage_update, "rowcount", None) == 1
                    owner_session.execute(
                        text("ALTER TABLE school_fact_change_lineage ENABLE ROW LEVEL SECURITY")
                    )
                    owner_session.execute(
                        text("ALTER TABLE school_fact_change_lineage FORCE ROW LEVEL SECURITY")
                    )
                    owner_session.execute(
                        text(
                            "ALTER TABLE school_fact_change_lineage ENABLE TRIGGER "
                            "trg_sf2_reject_lineage_update"
                        )
                    )
                    # Build a terminal, internally inconsistent ledger fixture without
                    # asking the production confirm path to create an invalid action.
                    # Both triggers are restored before the structure write is checked.
                    owner_session.execute(
                        text(
                            "ALTER TABLE master_data_import_units DISABLE TRIGGER "
                            "enforce_master_data_import_unit_immutability"
                        )
                    )
                    owner_session.execute(
                        text(
                            "ALTER TABLE master_data_import_units DISABLE TRIGGER "
                            "verify_master_data_import_units_update_terminal_consistency"
                        )
                    )
                    owner_session.execute(
                        text(
                            "UPDATE master_data_import_units SET "
                            "unit_kind = :unit_kind, target_id = :target_id, "
                            "target_version = :target_version, payload = "
                            "jsonb_set(jsonb_set(jsonb_set(jsonb_set(jsonb_set("
                            "payload, '{fact_kind}', to_jsonb(CAST(:payload_fact_kind AS text))), "
                            "'{source_key}', to_jsonb(CAST(:payload_source_key AS text))), "
                            "'{operation}', to_jsonb(CAST(:payload_operation AS text))), "
                            "'{effective_at}', "
                            "to_jsonb(CAST(:payload_effective_at AS timestamptz))), "
                            "'{expected_version}', "
                            "to_jsonb(CAST(:payload_expected_version AS bigint))) "
                            "WHERE school_id = :school_id AND id = :unit_id"
                        ),
                        {
                            "school_id": school_id,
                            "unit_id": original.unit_id,
                            "unit_kind": unit_kind,
                            "target_id": target_id,
                            "target_version": target_version,
                            "payload_fact_kind": payload_fact_kind,
                            "payload_source_key": payload_source_key,
                            "payload_operation": payload_operation,
                            "payload_effective_at": payload_effective_at,
                            "payload_expected_version": payload_expected_version,
                        },
                    )
                    owner_session.execute(
                        text(
                            "ALTER TABLE master_data_import_units ENABLE TRIGGER "
                            "verify_master_data_import_units_update_terminal_consistency"
                        )
                    )
                    owner_session.execute(
                        text(
                            "ALTER TABLE master_data_import_units ENABLE TRIGGER "
                            "enforce_master_data_import_unit_immutability"
                        )
                    )
                    forged_ledger = owner_session.execute(
                        text(
                            "SELECT lineage.fact_source_key, lineage.operation, "
                            "lineage.target_id, lineage.target_version, "
                            "batch.status AS batch_status, "
                            "unit.unit_kind, unit.status AS unit_status, "
                            "unit.target_id AS unit_target_id, "
                            "unit.target_version AS unit_target_version, unit.payload "
                            "FROM school_fact_change_lineage AS lineage "
                            "JOIN master_data_import_batches AS batch "
                            "ON batch.school_id = lineage.school_id "
                            "AND batch.id = lineage.batch_id "
                            "JOIN master_data_import_units AS unit "
                            "ON unit.school_id = lineage.school_id AND unit.id = lineage.unit_id "
                            "AND unit.batch_id = lineage.batch_id "
                            "WHERE lineage.school_id = :school_id AND lineage.id = :lineage_id"
                        ),
                        {"school_id": school_id, "lineage_id": original.lineage_id},
                    ).one()
                    assert forged_ledger.fact_source_key == lineage_source_key
                    assert forged_ledger.operation == "upsert"
                    assert forged_ledger.target_id == target_id
                    assert forged_ledger.target_version == target_version
                    assert forged_ledger.batch_status == "applied"
                    assert forged_ledger.unit_kind == unit_kind
                    assert forged_ledger.unit_status == "applied"
                    assert forged_ledger.unit_target_id == target_id
                    assert forged_ledger.unit_target_version == target_version
                    assert forged_ledger.payload["fact_kind"] == payload_fact_kind
                    assert forged_ledger.payload["source_key"] == payload_source_key
                    assert forged_ledger.payload["operation"] == payload_operation
                    if write_branch == "close_previous":
                        owner_session.execute(
                            text(
                                "UPDATE campus_location_versions SET valid_to = :effective_at "
                                "WHERE school_id = :school_id "
                                "AND location_id = :target_id AND version = 1"
                            ),
                            {
                                "school_id": school_id,
                                "target_id": target_id,
                                "effective_at": effective_at,
                            },
                        )
                    owner_session.execute(
                        text(
                            "INSERT INTO campus_location_versions ("
                            "id, school_id, location_id, parent_location_id, kind, "
                            "business_code, display_name, aliases, display_path, valid_from, "
                            "valid_to, source_kind, source_ref, status, version) VALUES ("
                            ":id, :school_id, :target_id, :parent_id, :target_kind, 'FORGED', "
                            "'伪造地点版本', ARRAY[]::varchar[], '/校区/伪造地点版本', "
                            ":effective_at, NULL, 'manual', :source_ref, 'confirmed', "
                            ":target_version)"
                        ),
                        {
                            "id": uuid4(),
                            "school_id": school_id,
                            "target_id": target_id,
                            "parent_id": campus_id,
                            "target_kind": target_kind,
                            "effective_at": effective_at,
                            "source_ref": f"change-set:{original.batch_id}",
                            "target_version": target_version,
                        },
                    )
                    owner_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            assert getattr(exc_info.value.orig, "sqlstate", None) == "23514"
            assert (
                _constraint_name(exc_info.value.orig)
                == "ck_campus_location_version_structure_lineage"
            )
    finally:
        runtime_engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("fact_kind", ("location", "organization"))
@pytest.mark.parametrize(
    "violation",
    ("expected_missing", "expected_null", "reactivate_v1"),
)
def test_structure_v1_requires_explicit_zero_expected_and_upsert(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
    violation: str,
) -> None:
    """A first content slice cannot masquerade as reactivate or omit its zero token."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, _user_id, _person_id, _assignment_id = facts
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: datetime.now(UTC),
        )
        _confirm_school_fact_plan(
            service,
            all_fact_kinds_change_set(school_source_key=_school_source_key(factory, school_id)),
            facts,
            label=f"structure-v1-{fact_kind}-{violation}",
        )
        effective_at = NOW + timedelta(days=1)
        target_id = uuid4()
        target_source_key = f"{fact_kind}:forged-v1:{uuid4().hex}"
        operation = "reactivate" if violation == "reactivate_v1" else "upsert"

        with pytest.raises(IntegrityError) as exc_info:
            with factory() as owner_session, owner_session.begin():
                if fact_kind == "location":
                    donor_source_key = "location:room-101"
                    parent_id = owner_session.execute(
                        text(
                            "SELECT id FROM campus_locations "
                            "WHERE school_id = :school_id "
                            "AND source_key = 'location:campus'"
                        ),
                        {"school_id": school_id},
                    ).scalar_one()
                    original = owner_session.execute(
                        text(
                            "SELECT lineage.id AS lineage_id, lineage.batch_id, "
                            "lineage.unit_id FROM campus_locations AS stable "
                            "JOIN school_fact_change_lineage AS lineage "
                            "ON lineage.school_id = stable.school_id "
                            "AND lineage.fact_kind = 'location' "
                            "AND lineage.fact_source_key = stable.source_key "
                            "AND lineage.target_id = stable.id "
                            "AND lineage.target_version = 1 "
                            "WHERE stable.school_id = :school_id "
                            "AND stable.source_key = :source_key"
                        ),
                        {"school_id": school_id, "source_key": donor_source_key},
                    ).one()
                    owner_session.execute(
                        text(
                            "INSERT INTO campus_locations ("
                            "id, school_id, source_key, parent_location_id, kind, "
                            "status, version) VALUES ("
                            ":id, :school_id, :source_key, :parent_id, 'building', "
                            "'active', 1)"
                        ),
                        {
                            "id": target_id,
                            "school_id": school_id,
                            "source_key": target_source_key,
                            "parent_id": parent_id,
                        },
                    )
                else:
                    donor_source_key = "organization:dorm"
                    parent_id = owner_session.execute(
                        text(
                            "SELECT id FROM organization_units "
                            "WHERE school_id = :school_id "
                            "AND source_key = :source_key"
                        ),
                        {"school_id": school_id, "source_key": donor_source_key},
                    ).scalar_one()
                    original = owner_session.execute(
                        text(
                            "SELECT lineage.id AS lineage_id, lineage.batch_id, "
                            "lineage.unit_id FROM organization_units AS stable "
                            "JOIN school_fact_change_lineage AS lineage "
                            "ON lineage.school_id = stable.school_id "
                            "AND lineage.fact_kind = 'organization' "
                            "AND lineage.fact_source_key = stable.source_key "
                            "AND lineage.target_id = stable.id "
                            "AND lineage.target_version = 1 "
                            "WHERE stable.school_id = :school_id "
                            "AND stable.source_key = :source_key"
                        ),
                        {"school_id": school_id, "source_key": donor_source_key},
                    ).one()
                    owner_session.execute(
                        text(
                            "INSERT INTO organization_units ("
                            "id, school_id, source_key, parent_organization_unit_id, kind, "
                            "status, version) VALUES ("
                            ":id, :school_id, :source_key, :parent_id, 'department', "
                            "'active', 1)"
                        ),
                        {
                            "id": target_id,
                            "school_id": school_id,
                            "source_key": target_source_key,
                            "parent_id": parent_id,
                        },
                    )

                owner_session.execute(
                    text(
                        "ALTER TABLE school_fact_change_lineage DISABLE TRIGGER "
                        "trg_sf2_reject_lineage_update"
                    )
                )
                owner_session.execute(
                    text("ALTER TABLE school_fact_change_lineage DISABLE ROW LEVEL SECURITY")
                )
                lineage_update = owner_session.execute(
                    text(
                        "UPDATE school_fact_change_lineage SET "
                        "fact_source_key = :source_key, operation = :operation, "
                        "target_id = :target_id, target_version = 1 "
                        "WHERE school_id = :school_id AND id = :lineage_id"
                    ),
                    {
                        "school_id": school_id,
                        "lineage_id": original.lineage_id,
                        "source_key": target_source_key,
                        "operation": operation,
                        "target_id": target_id,
                    },
                )
                assert getattr(lineage_update, "rowcount", None) == 1
                owner_session.execute(
                    text("ALTER TABLE school_fact_change_lineage ENABLE ROW LEVEL SECURITY")
                )
                owner_session.execute(
                    text("ALTER TABLE school_fact_change_lineage FORCE ROW LEVEL SECURITY")
                )
                owner_session.execute(
                    text(
                        "ALTER TABLE school_fact_change_lineage ENABLE TRIGGER "
                        "trg_sf2_reject_lineage_update"
                    )
                )
                owner_session.execute(
                    text(
                        "ALTER TABLE master_data_import_units DISABLE TRIGGER "
                        "enforce_master_data_import_unit_immutability"
                    )
                )
                owner_session.execute(
                    text(
                        "ALTER TABLE master_data_import_units DISABLE TRIGGER "
                        "verify_master_data_import_units_update_terminal_consistency"
                    )
                )
                unit_update = owner_session.execute(
                    text(
                        "UPDATE master_data_import_units SET "
                        "target_id = :target_id, target_version = 1, payload = "
                        "jsonb_set(jsonb_set(jsonb_set(jsonb_set(jsonb_set("
                        "payload, '{fact_kind}', to_jsonb(CAST(:fact_kind AS text))), "
                        "'{source_key}', to_jsonb(CAST(:source_key AS text))), "
                        "'{operation}', to_jsonb(CAST(:operation AS text))), "
                        "'{effective_at}', to_jsonb(CAST(:effective_at AS timestamptz))), "
                        "'{expected_version}', '0'::jsonb) "
                        "WHERE school_id = :school_id AND id = :unit_id"
                    ),
                    {
                        "school_id": school_id,
                        "unit_id": original.unit_id,
                        "target_id": target_id,
                        "fact_kind": fact_kind,
                        "source_key": target_source_key,
                        "operation": operation,
                        "effective_at": effective_at,
                    },
                )
                assert getattr(unit_update, "rowcount", None) == 1
                if violation == "expected_missing":
                    owner_session.execute(
                        text(
                            "UPDATE master_data_import_units SET "
                            "payload = payload - 'expected_version' "
                            "WHERE school_id = :school_id AND id = :unit_id"
                        ),
                        {"school_id": school_id, "unit_id": original.unit_id},
                    )
                elif violation == "expected_null":
                    owner_session.execute(
                        text(
                            "UPDATE master_data_import_units SET "
                            "payload = jsonb_set(payload, '{expected_version}', 'null'::jsonb) "
                            "WHERE school_id = :school_id AND id = :unit_id"
                        ),
                        {"school_id": school_id, "unit_id": original.unit_id},
                    )
                owner_session.execute(
                    text(
                        "ALTER TABLE master_data_import_units ENABLE TRIGGER "
                        "verify_master_data_import_units_update_terminal_consistency"
                    )
                )
                owner_session.execute(
                    text(
                        "ALTER TABLE master_data_import_units ENABLE TRIGGER "
                        "enforce_master_data_import_unit_immutability"
                    )
                )

                forged = owner_session.execute(
                    text(
                        "SELECT lineage.operation, unit.payload "
                        "FROM school_fact_change_lineage AS lineage "
                        "JOIN master_data_import_units AS unit "
                        "ON unit.school_id = lineage.school_id "
                        "AND unit.id = lineage.unit_id "
                        "WHERE lineage.school_id = :school_id "
                        "AND lineage.id = :lineage_id"
                    ),
                    {"school_id": school_id, "lineage_id": original.lineage_id},
                ).one()
                assert forged.operation == operation
                if violation == "expected_missing":
                    assert "expected_version" not in forged.payload
                elif violation == "expected_null":
                    assert forged.payload["expected_version"] is None
                else:
                    assert forged.payload["expected_version"] == 0

                if fact_kind == "location":
                    owner_session.execute(
                        text(
                            "INSERT INTO campus_location_versions ("
                            "id, school_id, location_id, parent_location_id, kind, "
                            "business_code, display_name, aliases, display_path, "
                            "valid_from, valid_to, source_kind, source_ref, status, version) "
                            "VALUES (:id, :school_id, :target_id, :parent_id, 'building', "
                            "'FORGED-V1', '伪造地点版本', ARRAY[]::varchar[], '/伪造地点版本', "
                            ":effective_at, NULL, 'manual', :source_ref, 'confirmed', 1)"
                        ),
                        {
                            "id": uuid4(),
                            "school_id": school_id,
                            "target_id": target_id,
                            "parent_id": parent_id,
                            "effective_at": effective_at,
                            "source_ref": f"change-set:{original.batch_id}",
                        },
                    )
                    expected_constraint = "ck_campus_location_version_structure_lineage"
                else:
                    owner_session.execute(
                        text(
                            "INSERT INTO organization_unit_versions ("
                            "id, school_id, organization_unit_id, "
                            "parent_organization_unit_id, business_code, display_name, "
                            "short_name, display_path, valid_from, valid_to, source_kind, "
                            "source_ref, status, version) VALUES ("
                            ":id, :school_id, :target_id, :parent_id, 'FORGED-V1', "
                            "'伪造组织版本', NULL, '/伪造组织版本', :effective_at, NULL, "
                            "'manual', :source_ref, 'confirmed', 1)"
                        ),
                        {
                            "id": uuid4(),
                            "school_id": school_id,
                            "target_id": target_id,
                            "parent_id": parent_id,
                            "effective_at": effective_at,
                            "source_ref": f"change-set:{original.batch_id}",
                        },
                    )
                    expected_constraint = "ck_organization_unit_version_structure_lineage"
                owner_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert getattr(exc_info.value.orig, "sqlstate", None) == "23514"
        assert _constraint_name(exc_info.value.orig) == expected_constraint


@pytest.mark.integration
def test_runtime_noop_location_structure_write_is_permission_denied(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Even a no-op structure write is denied at the runtime table ACL."""

    runtime_factory, runtime_engine = _runtime_factory(isolated_migration_schema)
    try:
        with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
            school_id, _user_id, _person_id, _assignment_id = facts
            service = SchoolFactChangeService(
                reader=persistence,
                writer=persistence,
                clock=lambda: datetime.now(UTC),
            )
            _confirm_school_fact_plan(
                service,
                all_fact_kinds_change_set(school_source_key=_school_source_key(factory, school_id)),
                facts,
                label="runtime-noop-location-structure",
            )
            envelope = _sf2_read_envelope(facts, label="runtime-noop-location-structure")
            with pytest.raises(DBAPIError) as exc_info:
                with SqlAlchemyUnitOfWork(runtime_factory, envelope) as uow:
                    uow.session.execute(
                        text(
                            "UPDATE campus_location_versions SET kind = kind "
                            "WHERE school_id = :school_id "
                            "AND location_id = ("
                            "SELECT id FROM campus_locations "
                            "WHERE school_id = :school_id "
                            "AND source_key = 'location:room-101') "
                            "AND valid_to IS NULL RETURNING 1"
                        ),
                        {"school_id": school_id},
                    )
            assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"
    finally:
        runtime_engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("connection_role", ("owner", "runtime"))
@pytest.mark.parametrize("student_status", ("active", "inactive"))
def test_student_person_identity_is_database_immutable_after_creation(
    isolated_migration_schema: PostgresRoles,
    connection_role: str,
    student_status: str,
) -> None:
    """Neither an owner nor runtime command may rebind a stable student identity."""

    runtime_factory, runtime_engine = _runtime_factory(isolated_migration_schema)
    try:
        with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
            school_id, _user_id, _person_id, _assignment_id = facts
            service = SchoolFactChangeService(
                reader=persistence,
                writer=persistence,
                clock=lambda: datetime.now(UTC),
            )
            _confirm_school_fact_plan(
                service,
                all_fact_kinds_change_set(school_source_key=_school_source_key(factory, school_id)),
                facts,
                label=f"student-person-immutable-{connection_role}-{student_status}",
            )
            if student_status == "inactive":
                _confirm_school_fact_plan(
                    service,
                    _terminal_plan(
                        base_version=2,
                        fact_kind="student",
                        source_key="student:001",
                        expected_version=1,
                        operation="end",
                        effective_at=datetime.now(UTC),
                    ),
                    facts,
                    label=f"student-person-inactive-{connection_role}",
                )

            params = {"school_id": school_id}
            statement = text(
                "UPDATE students SET person_id = ("
                "SELECT id FROM school_people WHERE school_id = :school_id "
                "AND source_key = 'person:parent') "
                "WHERE school_id = :school_id AND source_key = 'student:001'"
            )
            if connection_role == "owner":
                with factory() as owner_session:
                    with pytest.raises(IntegrityError) as exc_info:
                        with owner_session.begin():
                            owner_session.execute(statement, params)
                assert getattr(exc_info.value.orig, "sqlstate", None) == "23514"
                assert (
                    _constraint_name(exc_info.value.orig) == "ck_students_person_identity_immutable"
                )
            else:
                envelope = _sf2_read_envelope(
                    facts,
                    label=f"student-person-immutable-{student_status}",
                )
                with pytest.raises(DBAPIError) as exc_info:
                    with SqlAlchemyUnitOfWork(runtime_factory, envelope) as uow:
                        uow.session.execute(statement, params)
                assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"

            with factory() as owner_session:
                assert (
                    owner_session.execute(
                        text(
                            "SELECT count(*) FROM students "
                            "WHERE school_id = :school_id AND source_key = 'student:001'"
                        ),
                        params,
                    ).scalar_one()
                    == 1
                )
    finally:
        runtime_engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("operation", ("end", "revoke"))
def test_school_historical_reader_uses_content_versions_and_ledger_terminal(
    isolated_migration_schema: PostgresRoles,
    operation: str,
) -> None:
    """School history is a version slice plus its activation-epoch terminal lineage."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, _user_id, _person_id, _assignment_id = facts
        with factory() as owner_session:
            checked_now = owner_session.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one()
        assert isinstance(checked_now, datetime)
        source_key = _school_source_key(factory, school_id)
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: checked_now,
        )
        _confirm_school_fact_plan(
            service,
            all_fact_kinds_change_set(school_source_key=source_key),
            facts,
            label=f"snapshot-school-seed-{operation}",
        )
        terminal_at = checked_now + timedelta(hours=1)
        _confirm_school_fact_plan(
            service,
            _terminal_plan(
                base_version=2,
                fact_kind="school",
                source_key=source_key,
                expected_version=2,
                operation=operation,
                effective_at=terminal_at,
            ),
            facts,
            label=f"snapshot-school-{operation}",
        )
        reactivated_at = terminal_at + timedelta(seconds=1)
        if operation == "revoke":
            reactivation = CanonicalFactChangeSetV1.model_validate(
                {
                    "source_sha256": sha256(
                        f"snapshot-school-reactivate-{operation}".encode()
                    ).hexdigest(),
                    "base_version": 3,
                    "changes": (
                        {
                            "operation": "reactivate",
                            "fact_kind": "school",
                            "source_key": source_key,
                            "expected_version": 3,
                            "effective_at": reactivated_at,
                            "origin": {"sheet_code": "snapshot_reader", "row_number": 4},
                            "content": {
                                "business_code": "SF2-REACTIVATED",
                                "display_name": "重新启用后的学校",
                                "timezone_name": "Asia/Shanghai",
                                "lifecycle_status": "active",
                            },
                        },
                    ),
                }
            )
            _confirm_school_fact_plan(
                service,
                reactivation,
                facts,
                label=f"snapshot-school-reactivate-{operation}",
            )

        # Owner-only harness: prove historical School Facts semantics, not a retired runtime reader.
        with factory() as owner_session:
            repository = SqlAlchemySchoolRepository(owner_session)
            before = repository.get_current_school(
                SchoolId(school_id),
                as_of=terminal_at - timedelta(seconds=1),
            )
            between = repository.get_current_school(
                SchoolId(school_id),
                as_of=terminal_at + timedelta(milliseconds=500),
            )
            after = repository.get_current_school(
                SchoolId(school_id),
                as_of=reactivated_at + timedelta(seconds=1),
            )

    if operation == "end":
        assert before is not None and before.display_name == "SF2 全事实学校"
        assert after is None
    else:
        assert before is None
        assert after is not None and after.display_name == "重新启用后的学校"
    assert between is None


def _publish_renames(
    postgres_roles: PostgresRoles,
    facts: ExecutionFacts,
    renamed_at: datetime,
) -> None:
    marker = uuid4().hex
    with psycopg.connect(
        **postgres_roles.migration_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        connection.execute(
            "UPDATE school_person_names SET valid_to = %s "
            "WHERE person_id = %s AND status = 'confirmed' AND valid_to IS NULL",
            (renamed_at, facts.student_person_id),
        )
        connection.execute(
            """
            INSERT INTO school_person_names (
                id, school_id, person_id, display_name, valid_from,
                source_kind, source_ref, status, version
            ) VALUES (%s, %s, %s, '新姓名', %s, 'manual', %s, 'confirmed', 2)
            """,
            (uuid4(), facts.school_id, facts.student_person_id, renamed_at, marker),
        )
        connection.commit()

    owner_factory, engine = make_owner_factory(postgres_roles)
    try:
        with owner_factory() as session:
            location_source_key = str(
                session.execute(
                    text("SELECT source_key FROM campus_locations WHERE id = :location_id"),
                    {"location_id": facts.location_id},
                ).scalar_one()
            )
            base_version = int(
                session.execute(
                    text(
                        "SELECT COALESCE(max(target_fact_version), 1) "
                        "FROM master_data_import_batches "
                        "WHERE school_id = :school_id "
                        "AND mode = 'canonical_atomic_changes' "
                        "AND status IN ('applied', 'published')"
                    ),
                    {"school_id": facts.school_id},
                ).scalar_one()
            )
        signer = ContextProofSigner(_CONTEXT_KEY)
        persistence = SqlAlchemySchoolFactPersistence(
            owner_factory,
            lambda context: signer.sign_execution_context(
                context,
                session_nonce_hash=sha256(b"snapshot-rename-session").hexdigest(),
                proof_expires_at=datetime.now(UTC) + timedelta(minutes=30),
            ),
            confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                CONFIRMATION_SIGNING_KEY
            ),
        )
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: renamed_at,
        )
        changes = CanonicalFactChangeSetV1.model_validate(
            {
                "source_sha256": sha256(f"snapshot-rename:{marker}".encode()).hexdigest(),
                "base_version": base_version,
                "changes": (
                    {
                        "operation": "upsert",
                        "fact_kind": "location",
                        "source_key": location_source_key,
                        "expected_version": 1,
                        "effective_at": renamed_at,
                        "origin": {"sheet_code": "snapshot_reader", "row_number": 2},
                        "content": {
                            "kind": "campus",
                            "display_name": "新校区名",
                            "display_path": "Task 6 学校 / 新校区名",
                            "aliases": (),
                            "status": "active",
                        },
                    },
                ),
            }
        )
        context = context_for(
            school_id=facts.school_id,
            user_id=facts.user_id,
            person_id=facts.person_id,
            assignment_id=facts.assignment_id,
            key=f"snapshot-rename:{marker}",
        )
        receipt = service.prepare_confirmation(
            change_set=changes,
            context=context,
            session_binding="snapshot-rename-session",
        )
        service.confirm(
            credential=receipt.credential,
            change_set=changes,
            context=context,
            session_binding="snapshot-rename-session",
        )
    finally:
        engine.dispose()  # type: ignore[union-attr]


def _school_source_key(factory: sessionmaker[Session], school_id: UUID) -> str:
    with factory() as session:
        return str(
            session.execute(
                text("SELECT source_key FROM schools WHERE id = :school_id"),
                {"school_id": school_id},
            ).scalar_one()
        )


def _runtime_factory(
    roles: PostgresRoles,
) -> tuple[sessionmaker[Session], Engine]:
    runtime = roles.runtime_connection_parameters
    engine = create_engine(
        URL.create(
            drivername="postgresql+psycopg",
            username=str(runtime["user"]),
            password=str(runtime["password"]),
            host=str(runtime["host"]),
            port=int(runtime["port"]),
            database=str(runtime["dbname"]),
        ),
        poolclass=NullPool,
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False), engine


def _terminal_plan(
    *,
    base_version: int,
    fact_kind: str,
    source_key: str,
    expected_version: int,
    operation: str,
    effective_at: datetime,
) -> CanonicalFactChangeSetV1:
    return CanonicalFactChangeSetV1.model_validate(
        {
            "source_sha256": sha256(
                f"snapshot:{fact_kind}:{operation}:{source_key}".encode()
            ).hexdigest(),
            "base_version": base_version,
            "changes": (
                {
                    "operation": operation,
                    "fact_kind": fact_kind,
                    "source_key": source_key,
                    "expected_version": expected_version,
                    "effective_at": effective_at,
                    "origin": {"sheet_code": "snapshot_reader", "row_number": 1},
                    "content": None,
                },
            ),
        }
    )


def _confirm_school_fact_plan(
    service: SchoolFactChangeService,
    plan: CanonicalFactChangeSetV1,
    facts: tuple[UUID, int, UUID, UUID],
    *,
    label: str,
):
    school_id, user_id, person_id, assignment_id = facts
    context = context_for(
        school_id=school_id,
        user_id=user_id,
        person_id=person_id,
        assignment_id=assignment_id,
        key=f"snapshot:{label}:{uuid4().hex}",
    )
    session_binding = f"snapshot-{label}-{uuid4().hex}"
    receipt = service.prepare_confirmation(
        change_set=plan,
        context=context,
        session_binding=session_binding,
    )
    result = service.confirm(
        credential=receipt.credential,
        change_set=plan,
        context=context,
        session_binding=session_binding,
    )
    assert result.changed_count == len(plan.changes)
    return result


def _sf2_read_envelope(facts: tuple[UUID, int, UUID, UUID], *, label: str):
    school_id, user_id, person_id, assignment_id = facts
    context = context_for(
        school_id=school_id,
        user_id=user_id,
        person_id=person_id,
        assignment_id=assignment_id,
        key=f"snapshot-read:{label}:{uuid4().hex}",
    )
    return ContextProofSigner(_CONTEXT_KEY).sign_execution_context(
        context,
        session_nonce_hash=sha256(b"snapshot-reader-session").hexdigest(),
        proof_expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
