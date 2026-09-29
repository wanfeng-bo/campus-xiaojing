from __future__ import annotations

import inspect
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Table, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.domains.students.models import StudentBedResidence
from app.platform.contracts import SchoolId
from app.platform.master_data_imports.school_facts_excel import SCHOOL_FACT_SHEET_SPECS
from app.platform.school_facts.application import (
    SchoolFactChangeRejected,
    SchoolFactChangeService,
)
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    CanonicalFactContentV1,
)
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_checkpoint2_lifecycle_red import (
    _confirm,
    _plan,
    _school_source_key,
)
from tests.school_facts.test_sf2_transaction import (
    NOW,
    all_fact_kinds_change_set,
    context_for,
    sf2_environment,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
FORBIDDEN_EDGE_FIELDS = {
    "arrived_from_residence_id",
    "departed_to_residence_id",
    "arrived_from_source_key",
    "departed_to_source_key",
}


def test_residence_order_dependencies_are_linear_at_the_change_set_cap() -> None:
    """50k independent students must not create an end-by-upsert Cartesian product."""

    transition_count = 25_000
    terminals: list[object] = []
    upserts: list[object] = []
    students_by_terminal: dict[str, str] = {}
    for index in range(transition_count):
        student_source_key = f"student:scale:{index}"
        terminal_source_key = f"residence:scale:old:{index}"
        terminals.append(
            SimpleNamespace(
                fact_kind="student_bed_residence",
                operation="end",
                source_key=terminal_source_key,
                effective_at=NOW + timedelta(seconds=index),
                content=None,
            )
        )
        upserts.append(
            SimpleNamespace(
                fact_kind="student_bed_residence",
                operation="upsert",
                source_key=f"residence:scale:new:{index}",
                effective_at=NOW + timedelta(seconds=index),
                content=SimpleNamespace(student_source_key=student_source_key),
            )
        )
        students_by_terminal[terminal_source_key] = student_source_key

    changes = cast(
        tuple[CanonicalFactChangeV1, ...],
        tuple((*terminals, *upserts)),
    )
    dependencies = SqlAlchemySchoolFactPersistence._residence_order_dependencies(
        changes,
        students_by_terminal,
    )

    assert len(changes) == 50_000
    assert len(dependencies) == transition_count
    assert (0, transition_count) in dependencies
    assert (transition_count - 1, len(changes) - 1) in dependencies
    assert all(upsert - terminal == transition_count for terminal, upsert in dependencies)


@dataclass(slots=True)
class _ResidenceHarness:
    factory: sessionmaker[Session]
    persistence: SqlAlchemySchoolFactPersistence
    service: SchoolFactChangeService
    school_id: UUID
    user_id: int
    person_id: UUID
    assignment_id: UUID
    clock_at: datetime = NOW
    version: int = 2

    def confirm(self, change_set: CanonicalFactChangeSetV1, *, label: str):
        result = _confirm(
            service=self.service,
            change_set=change_set,
            school_id=self.school_id,
            user_id=self.user_id,
            person_id=self.person_id,
            assignment_id=self.assignment_id,
            label=label,
        )
        self.version = result.target_version
        return result

    def reader(self) -> SqlAlchemySchoolFactPersistence:
        return self.persistence.for_request(
            context_for(
                school_id=self.school_id,
                user_id=self.user_id,
                person_id=self.person_id,
                assignment_id=self.assignment_id,
                key=f"residence-reader:{uuid4().hex}",
            )
        )

    def preflight(
        self,
        *,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
    ):
        return SchoolFactChangeService(
            reader=self.reader(),
            writer=self.persistence,
            clock=lambda: self.clock_at,
        ).preflight(school_id=school_id, change_set=change_set)


@contextmanager
def _residence_harness(
    roles: PostgresRoles,
    *,
    include_initial_residence: bool = True,
    seed_effective_at: datetime | None = None,
    clock_at: datetime | None = None,
) -> Iterator[_ResidenceHarness]:
    with sf2_environment(roles) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        if seed_effective_at is not None:
            with factory.begin() as session:
                session.execute(
                    text(
                        "UPDATE school_versions SET valid_from = :valid_from "
                        "WHERE school_id = :school_id AND version = 1"
                    ),
                    {
                        "school_id": school_id,
                        "valid_from": seed_effective_at - timedelta(days=1),
                    },
                )
        service_now = clock_at or NOW
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: service_now,
        )
        seed = all_fact_kinds_change_set(school_source_key=_school_source_key(factory, school_id))
        if not include_initial_residence or seed_effective_at is not None:
            payload = seed.model_dump(mode="python", exclude_none=True)
            if not include_initial_residence:
                payload["changes"] = tuple(
                    change
                    for change in payload["changes"]
                    if change["fact_kind"] != "student_bed_residence"
                )
            if seed_effective_at is not None:
                for change in payload["changes"]:
                    change["effective_at"] = seed_effective_at
            payload["source_sha256"] = "1" * 64
            seed = CanonicalFactChangeSetV1.model_validate(payload)
        harness = _ResidenceHarness(
            factory=factory,
            persistence=persistence,
            service=service,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            clock_at=service_now,
        )
        result = harness.confirm(seed, label="residence-seed")
        assert result.target_version == 2
        yield harness


def _residence_change(
    *,
    operation: str,
    source_key: str,
    expected_version: int,
    effective_at,
    row_number: int,
    bed_source_key: str | None = None,
    valid_to=None,
    legacy_arrived_from: str | None = None,
) -> dict[str, object]:
    content: dict[str, object] | None = None
    if operation == "upsert":
        assert bed_source_key is not None
        content = {
            "student_source_key": "student:001",
            "bed_source_key": bed_source_key,
        }
        if valid_to is not None:
            content["valid_to"] = valid_to
        if (
            legacy_arrived_from is not None
            and "arrived_from_source_key" in CanonicalFactContentV1.model_fields
        ):
            # Compatibility-only setup for the pre-Checkpoint-3 schema. The RED assertions
            # below never require this field in the corrected public contract.
            content["arrived_from_source_key"] = legacy_arrived_from
    return {
        "operation": operation,
        "fact_kind": "student_bed_residence",
        "source_key": source_key,
        "expected_version": expected_version,
        "effective_at": effective_at,
        "origin": {"sheet_code": "student_bed_residences", "row_number": row_number},
        "content": content,
    }


def _add_beds(harness: _ResidenceHarness, *source_keys: str) -> None:
    changes = tuple(
        {
            "operation": "upsert",
            "fact_kind": "dorm_bed",
            "source_key": source_key,
            "expected_version": 0,
            "effective_at": NOW,
            "origin": {"sheet_code": "dorm_beds", "row_number": index + 2},
            "content": {
                "room_source_key": "location:room-101",
                "business_code": source_key.rsplit(":", 1)[-1].upper(),
                "display_name": source_key,
                "availability": "available",
            },
        }
        for index, source_key in enumerate(source_keys)
    )
    harness.confirm(
        _plan(
            label=f"add-beds-{'-'.join(source_keys)}",
            base_version=harness.version,
            changes=changes,
        ),
        label="add-beds",
    )


def _assert_batch_cardinality(
    harness: _ResidenceHarness,
    *,
    batch_id: UUID,
    units: int,
    lineage: int,
) -> None:
    with harness.factory() as session:  # type: ignore[operator]
        counts = session.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM master_data_import_units WHERE batch_id = :batch_id), "
                "(SELECT count(*) FROM school_fact_change_lineage WHERE batch_id = :batch_id)"
            ),
            {"batch_id": batch_id},
        ).one()
    assert counts == (units, lineage)


def _residence_row(harness: _ResidenceHarness, source_key: str) -> tuple[object, ...]:
    with harness.factory() as session:  # type: ignore[operator]
        return tuple(
            session.execute(
                text(
                    "SELECT id, student_id, bed_id, valid_from, valid_to, revoked_at, version "
                    "FROM student_bed_residences WHERE id = ("
                    "SELECT target_id FROM school_fact_change_lineage "
                    "WHERE school_id = :school_id "
                    "AND fact_kind = 'student_bed_residence' "
                    "AND fact_source_key = :source_key "
                    "ORDER BY target_version DESC LIMIT 1)"
                ),
                {"school_id": harness.school_id, "source_key": source_key},
            ).one()
        )


def test_residence_orm_has_no_self_fk_columns_or_constraints() -> None:
    table = cast(Table, StudentBedResidence.__table__)
    assert FORBIDDEN_EDGE_FIELDS.isdisjoint(table.c.keys()), (
        f"Residence ORM 仍公开前后事实字段：{sorted(FORBIDDEN_EDGE_FIELDS & set(table.c.keys()))}"
    )
    artifact_text = "\n".join(
        [str(constraint.name or "") for constraint in table.constraints]
        + [str(constraint) for constraint in table.constraints]
    )
    assert not any(token in artifact_text for token in FORBIDDEN_EDGE_FIELDS), (
        "Residence ORM 仍保留 self-FK/唯一/CHECK 约束"
    )


def test_current_head_postgres_has_no_residence_self_fk_columns(
    current_master_data_schema: PostgresRoles,
) -> None:
    with current_master_data_schema.migration_connection() as connection:
        columns = {
            str(row[0])
            for row in connection.execute(
                "SELECT attname FROM pg_attribute "
                "WHERE attrelid = 'public.student_bed_residences'::regclass "
                "AND attnum > 0 AND NOT attisdropped"
            ).fetchall()
        }
    forbidden_columns = FORBIDDEN_EDGE_FIELDS & columns
    assert not forbidden_columns, f"current-head PostgreSQL 仍有列 {sorted(forbidden_columns)}"


def test_current_head_postgres_has_no_residence_self_fk_or_transfer_trigger(
    current_master_data_schema: PostgresRoles,
) -> None:
    with current_master_data_schema.migration_connection() as connection:
        constraints = connection.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'public.student_bed_residences'::regclass"
        ).fetchall()
        triggers = connection.execute(
            "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
            "WHERE tgrelid = 'public.student_bed_residences'::regclass "
            "AND NOT tgisinternal"
        ).fetchall()
    flattened = "\n".join(str(item) for row in (*constraints, *triggers) for item in row)
    assert not any(token in flattened for token in FORBIDDEN_EDGE_FIELDS), (
        "current-head PostgreSQL 仍有 Residence self-FK/互反约束或 trigger"
    )
    assert "atomic_transfer" not in flattened, (
        "current-head PostgreSQL 仍有 atomic transfer trigger"
    )


def test_residence_public_contract_rejects_edge_source_keys() -> None:
    base = {
        "operation": "upsert",
        "fact_kind": "student_bed_residence",
        "source_key": "residence:contract-edge",
        "expected_version": 0,
        "effective_at": NOW,
        "origin": {"sheet_code": "student_bed_residences", "row_number": 2},
        "content": {
            "student_source_key": "student:001",
            "bed_source_key": "dorm-bed:101-1",
        },
    }
    accepted: list[str] = []
    for field in ("arrived_from_source_key", "departed_to_source_key"):
        payload = {**base, "content": {**base["content"], field: "residence:other"}}
        try:
            CanonicalFactChangeSetV1.model_validate(
                {"source_sha256": "2" * 64, "base_version": 1, "changes": (payload,)}
            )
        except ValueError:
            continue
        accepted.append(field)
    assert not accepted, f"Residence 公共 canonical contract 仍接受 {accepted}"


def test_residence_excel_edge_columns_were_removed_by_checkpoint4() -> None:
    fields = {
        field_code for _header, field_code in SCHOOL_FACT_SHEET_SPECS["12_学生入住"].content_headers
    }
    assert fields.isdisjoint(FORBIDDEN_EDGE_FIELDS)


def test_residence_admin_ui_and_types_removed_checkpoint4_edge_debt() -> None:
    paths = (
        REPO_ROOT / "apps/web/src/features/school_facts/SchoolFactAdmin.vue",
        REPO_ROOT / "apps/web/src/features/school_facts/school_fact_contracts.ts",
    )
    offenders = {
        path.relative_to(REPO_ROOT).as_posix(): sorted(
            token for token in FORBIDDEN_EDGE_FIELDS if token in path.read_text(encoding="utf-8")
        )
        for path in paths
    }
    offenders = {path: fields for path, fields in offenders.items() if fields}
    assert offenders == {}


def test_residence_application_and_writer_have_no_edge_semantics() -> None:
    application_source = (
        REPO_ROOT / "apps/api/app/platform/school_facts/application.py"
    ).read_text(encoding="utf-8")
    writer_source = inspect.getsource(SqlAlchemySchoolFactPersistence)
    offenders = {
        token
        for token in FORBIDDEN_EDGE_FIELDS
        if token in application_source or token in writer_source
    }
    assert not offenders, f"应用预检或写核仍保留 Residence edge 语义：{sorted(offenders)}"


def test_same_bed_continuous_slice_is_two_units_two_lineage_one_confirmation(
    current_master_data_schema: PostgresRoles,
) -> None:
    with _residence_harness(current_master_data_schema) as harness:
        boundary = NOW + timedelta(days=1)
        plan = _plan(
            label="same-bed-continuous-slice",
            base_version=harness.version,
            changes=(
                _residence_change(
                    operation="upsert",
                    source_key="residence:student-001:slice-2",
                    expected_version=0,
                    effective_at=boundary,
                    row_number=3,
                    bed_source_key="dorm-bed:101-1",
                ),
                _residence_change(
                    operation="end",
                    source_key="residence:student-001",
                    expected_version=1,
                    effective_at=boundary,
                    row_number=2,
                ),
            ),
        )
        report = harness.preflight(school_id=SchoolId(harness.school_id), change_set=plan)
        assert report.is_clean, (
            "同一稳定床位连续切片无需 Residence edge，预检却拒绝："
            f"{[(issue.column_code, issue.error_code) for issue in report.issues]}"
        )
        result = harness.confirm(plan, label="same-bed-continuous-slice")
        assert result.changed_count == 2
        _assert_batch_cardinality(harness, batch_id=result.batch_id, units=2, lineage=2)
        old_row = _residence_row(harness, "residence:student-001")
        new_row = _residence_row(harness, "residence:student-001:slice-2")
        assert old_row[4] == boundary
        assert new_row[3] == boundary
        assert old_row[2] == new_row[2]


def test_different_bed_transfer_is_two_units_two_lineage_one_confirmation(
    current_master_data_schema: PostgresRoles,
) -> None:
    with _residence_harness(current_master_data_schema) as harness:
        _add_beds(harness, "dorm-bed:101-2")
        boundary = NOW + timedelta(days=1)
        plan = _plan(
            label="different-bed-transfer",
            base_version=harness.version,
            changes=(
                _residence_change(
                    operation="end",
                    source_key="residence:student-001",
                    expected_version=1,
                    effective_at=boundary,
                    row_number=2,
                ),
                _residence_change(
                    operation="upsert",
                    source_key="residence:student-001:bed-2",
                    expected_version=0,
                    effective_at=boundary,
                    row_number=3,
                    bed_source_key="dorm-bed:101-2",
                ),
            ),
        )
        report = harness.preflight(school_id=SchoolId(harness.school_id), change_set=plan)
        assert report.is_clean, (
            "异床调宿由两个显式 unit 配对，不应要求 Residence edge："
            f"{[(issue.column_code, issue.error_code) for issue in report.issues]}"
        )
        result = harness.confirm(plan, label="different-bed-transfer")
        assert result.changed_count == 2
        _assert_batch_cardinality(harness, batch_id=result.batch_id, units=2, lineage=2)
        old_row = _residence_row(harness, "residence:student-001")
        new_row = _residence_row(harness, "residence:student-001:bed-2")
        assert old_row[4] == boundary
        assert new_row[3] == boundary
        assert new_row[4] is None
        assert old_row[2] != new_row[2]


def test_checkout_is_one_unit_one_lineage_one_confirmation(
    current_master_data_schema: PostgresRoles,
) -> None:
    with _residence_harness(current_master_data_schema) as harness:
        checkout_at = NOW + timedelta(days=1)
        plan = _plan(
            label="plain-checkout",
            base_version=harness.version,
            changes=(
                _residence_change(
                    operation="end",
                    source_key="residence:student-001",
                    expected_version=1,
                    effective_at=checkout_at,
                    row_number=2,
                ),
            ),
        )
        result = harness.confirm(plan, label="plain-checkout")
        assert result.changed_count == 1
        _assert_batch_cardinality(harness, batch_id=result.batch_id, units=1, lineage=1)
        assert _residence_row(harness, "residence:student-001")[4] == checkout_at


def test_future_effective_residence_is_one_unit_one_lineage_one_confirmation(
    current_master_data_schema: PostgresRoles,
) -> None:
    with _residence_harness(current_master_data_schema, include_initial_residence=False) as harness:
        future_at = NOW + timedelta(days=30)
        plan = _plan(
            label="future-effective-residence",
            base_version=harness.version,
            changes=(
                _residence_change(
                    operation="upsert",
                    source_key="residence:student-001:future",
                    expected_version=0,
                    effective_at=future_at,
                    row_number=2,
                    bed_source_key="dorm-bed:101-1",
                ),
            ),
        )
        result = harness.confirm(plan, label="future-effective-residence")
        assert result.changed_count == 1
        _assert_batch_cardinality(harness, batch_id=result.batch_id, units=1, lineage=1)
        row = _residence_row(harness, "residence:student-001:future")
        assert row[3] == future_at
        assert row[4] is None


def test_bounded_historical_residence_is_one_unit_one_lineage_one_confirmation(
    current_master_data_schema: PostgresRoles,
) -> None:
    valid_from = NOW - timedelta(days=60)
    valid_to = NOW - timedelta(days=30)
    with _residence_harness(
        current_master_data_schema,
        include_initial_residence=False,
        seed_effective_at=valid_from - timedelta(days=1),
    ) as harness:
        plan = _plan(
            label="bounded-historical-residence",
            base_version=harness.version,
            changes=(
                _residence_change(
                    operation="upsert",
                    source_key="residence:student-001:historical",
                    expected_version=0,
                    effective_at=valid_from,
                    row_number=2,
                    bed_source_key="dorm-bed:101-1",
                    valid_to=valid_to,
                ),
            ),
        )
        result = harness.confirm(plan, label="bounded-historical-residence")
        assert result.changed_count == 1
        _assert_batch_cardinality(harness, batch_id=result.batch_id, units=1, lineage=1)
        row = _residence_row(harness, "residence:student-001:historical")
        assert (row[3], row[4]) == (valid_from, valid_to)


def test_a_to_b_to_c_uses_formal_channel_with_four_transfer_units_and_lineage(
    current_master_data_schema: PostgresRoles,
) -> None:
    with _residence_harness(current_master_data_schema) as harness:
        _add_beds(harness, "dorm-bed:101-2", "dorm-bed:101-3")
        first_boundary = NOW + timedelta(days=1)
        first = _plan(
            label="formal-transfer-a-b",
            base_version=harness.version,
            changes=(
                _residence_change(
                    operation="end",
                    source_key="residence:student-001",
                    expected_version=1,
                    effective_at=first_boundary,
                    row_number=2,
                ),
                _residence_change(
                    operation="upsert",
                    source_key="residence:student-001:b",
                    expected_version=0,
                    effective_at=first_boundary,
                    row_number=3,
                    bed_source_key="dorm-bed:101-2",
                ),
            ),
        )
        first_report = harness.preflight(school_id=SchoolId(harness.school_id), change_set=first)
        assert first_report.is_clean, (
            "A→B 的两个显式 unit 无需 Residence edge，正式预检却拒绝："
            f"{[(issue.column_code, issue.error_code) for issue in first_report.issues]}"
        )
        first_result = harness.confirm(first, label="formal-transfer-a-b")

        second_boundary = NOW + timedelta(days=2)
        second = _plan(
            label="formal-transfer-b-c",
            base_version=harness.version,
            changes=(
                _residence_change(
                    operation="end",
                    source_key="residence:student-001:b",
                    expected_version=1,
                    effective_at=second_boundary,
                    row_number=2,
                ),
                _residence_change(
                    operation="upsert",
                    source_key="residence:student-001:c",
                    expected_version=0,
                    effective_at=second_boundary,
                    row_number=3,
                    bed_source_key="dorm-bed:101-3",
                ),
            ),
        )
        second_report = harness.preflight(school_id=SchoolId(harness.school_id), change_set=second)
        assert second_report.is_clean, (
            "B→C 的两个显式 unit 无需 Residence edge，正式预检却拒绝："
            f"{[(issue.column_code, issue.error_code) for issue in second_report.issues]}"
        )
        second_result = harness.confirm(second, label="formal-transfer-b-c")

        assert first_result.changed_count == second_result.changed_count == 2
        with harness.factory() as session:  # type: ignore[operator]
            counts = session.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM master_data_import_units "
                    " WHERE batch_id IN (:first, :second)), "
                    "(SELECT count(*) FROM school_fact_change_lineage "
                    " WHERE batch_id IN (:first, :second))"
                ),
                {"first": first_result.batch_id, "second": second_result.batch_id},
            ).one()
        assert counts == (4, 4)
        assert first_result.batch_id != second_result.batch_id
        assert _residence_row(harness, "residence:student-001")[4] == first_boundary
        assert _residence_row(harness, "residence:student-001:b")[4] == second_boundary
        assert _residence_row(harness, "residence:student-001:c")[4] is None


def test_residence_upsert_never_implicitly_updates_another_fact_row(
    current_master_data_schema: PostgresRoles,
) -> None:
    with _residence_harness(current_master_data_schema) as harness:
        _add_beds(harness, "dorm-bed:101-2")
        boundary = NOW + timedelta(days=1)
        statements: list[str] = []
        engine = cast(Engine, harness.persistence._session_factory.kw["bind"])  # noqa: SLF001

        def record_statement(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany: bool,
        ) -> None:
            statements.append(" ".join(statement.split()))

        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            result = _legacy_compatible_transfer(
                harness,
                old_source_key="residence:student-001",
                new_source_key="residence:student-001:bed-2",
                new_bed_source_key="dorm-bed:101-2",
                boundary=boundary,
                base_version=harness.version,
                old_expected_version=1,
                label="observe-residence-row-writes",
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)

        residence_updates = [
            statement
            for statement in statements
            if statement.startswith("UPDATE student_bed_residences")
        ]
        assert len(residence_updates) == 1, (
            "两个 unit 的调宿只能由显式 end 更新旧行；upsert 不得再次隐藏更新旧行。"
            f"实际 Residence UPDATE={residence_updates}"
        )
        _assert_batch_cardinality(harness, batch_id=result.batch_id, units=2, lineage=2)
        old_row = _residence_row(harness, "residence:student-001")
        new_row = _residence_row(harness, "residence:student-001:bed-2")
        with harness.factory() as session:  # type: ignore[operator]
            lineage_targets = {
                str(source_key): UUID(str(target_id))
                for source_key, target_id in session.execute(
                    text(
                        "SELECT fact_source_key, target_id "
                        "FROM school_fact_change_lineage WHERE batch_id = :batch_id"
                    ),
                    {"batch_id": result.batch_id},
                ).all()
            }
        assert lineage_targets == {
            "residence:student-001": old_row[0],
            "residence:student-001:bed-2": new_row[0],
        }
        assert old_row[0] != new_row[0], "end/upsert 必须各有独立事实行与 lineage"


def _legacy_compatible_transfer(
    harness: _ResidenceHarness,
    *,
    old_source_key: str,
    new_source_key: str,
    new_bed_source_key: str,
    boundary,
    base_version: int,
    old_expected_version: int,
    label: str,
):
    plan = _plan(
        label=label,
        base_version=base_version,
        changes=(
            _residence_change(
                operation="end",
                source_key=old_source_key,
                expected_version=old_expected_version,
                effective_at=boundary,
                row_number=2,
            ),
            _residence_change(
                operation="upsert",
                source_key=new_source_key,
                expected_version=0,
                effective_at=boundary,
                row_number=3,
                bed_source_key=new_bed_source_key,
                legacy_arrived_from=old_source_key,
            ),
        ),
    )
    return harness.confirm(plan, label=label)


def test_middle_residence_correction_does_not_rewrite_neighbor_intervals(
    current_master_data_schema: PostgresRoles,
) -> None:
    with _residence_harness(current_master_data_schema) as harness:
        _add_beds(
            harness,
            "dorm-bed:101-2",
            "dorm-bed:101-3",
            "dorm-bed:101-4",
        )
        first_boundary = NOW + timedelta(days=1)
        second_boundary = NOW + timedelta(days=2)
        _legacy_compatible_transfer(
            harness,
            old_source_key="residence:student-001",
            new_source_key="residence:student-001:b",
            new_bed_source_key="dorm-bed:101-2",
            boundary=first_boundary,
            base_version=harness.version,
            old_expected_version=1,
            label="middle-correction-seed-a-b",
        )
        _legacy_compatible_transfer(
            harness,
            old_source_key="residence:student-001:b",
            new_source_key="residence:student-001:c",
            new_bed_source_key="dorm-bed:101-3",
            boundary=second_boundary,
            base_version=harness.version,
            old_expected_version=1,
            label="middle-correction-seed-b-c",
        )
        before_a = _residence_row(harness, "residence:student-001")
        before_b = _residence_row(harness, "residence:student-001:b")
        before_c = _residence_row(harness, "residence:student-001:c")
        correction_at = NOW + timedelta(days=3)
        correction = _plan(
            label="middle-residence-correction",
            base_version=harness.version,
            changes=(
                _residence_change(
                    operation="revoke",
                    source_key="residence:student-001:b",
                    expected_version=2,
                    effective_at=correction_at,
                    row_number=2,
                ),
                _residence_change(
                    operation="upsert",
                    source_key="residence:student-001:b-corrected",
                    expected_version=0,
                    effective_at=first_boundary,
                    row_number=3,
                    bed_source_key="dorm-bed:101-4",
                    valid_to=second_boundary,
                ),
            ),
        )
        result = harness.confirm(correction, label="middle-residence-correction")

        assert result.changed_count == 2
        _assert_batch_cardinality(harness, batch_id=result.batch_id, units=2, lineage=2)
        assert _residence_row(harness, "residence:student-001") == before_a
        assert _residence_row(harness, "residence:student-001:c") == before_c
        corrected = _residence_row(harness, "residence:student-001:b-corrected")
        assert (corrected[3], corrected[4]) == (first_boundary, second_boundary)
        assert corrected[2] != before_b[2]


@pytest.mark.parametrize(
    ("first_operation", "second_operation"),
    (
        ("end", "end"),
        ("revoke", "end"),
        ("revoke", "revoke"),
    ),
)
def test_residence_rejects_every_other_repeated_terminal_combination(
    current_master_data_schema: PostgresRoles,
    first_operation: str,
    second_operation: str,
) -> None:
    with _residence_harness(current_master_data_schema) as harness:
        source_key = "residence:student-001"
        first_at = NOW + timedelta(days=1)
        harness.confirm(
            _plan(
                label=f"residence-first-{first_operation}",
                base_version=harness.version,
                changes=(
                    _residence_change(
                        operation=first_operation,
                        source_key=source_key,
                        expected_version=1,
                        effective_at=first_at,
                        row_number=2,
                    ),
                ),
            ),
            label=f"residence-first-{first_operation}",
        )
        before = _residence_row(harness, source_key)

        with pytest.raises(
            SchoolFactChangeRejected,
            match="已终止的事实不能再次结束或撤销",
        ):
            harness.confirm(
                _plan(
                    label=f"residence-{first_operation}-then-{second_operation}",
                    base_version=harness.version,
                    changes=(
                        _residence_change(
                            operation=second_operation,
                            source_key=source_key,
                            expected_version=2,
                            effective_at=first_at + timedelta(days=1),
                            row_number=2,
                        ),
                    ),
                ),
                label=f"residence-{first_operation}-then-{second_operation}",
            )

        assert _residence_row(harness, source_key) == before
        with harness.factory() as session:  # type: ignore[operator]
            lineage = session.execute(
                text(
                    "SELECT target_version, operation FROM school_fact_change_lineage "
                    "WHERE school_id = :school_id "
                    "AND fact_kind = 'student_bed_residence' "
                    "AND fact_source_key = :source_key ORDER BY target_version"
                ),
                {"school_id": harness.school_id, "source_key": source_key},
            ).all()
        assert lineage == [(1, "upsert"), (2, first_operation)]
