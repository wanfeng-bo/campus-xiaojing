from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, Table

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.db.base import Base
from app.domains.campus import models as campus_models  # noqa: F401
from app.domains.students import models as student_models  # noqa: F401
from app.platform.contracts import CommandExecutionContext, SchoolId
from app.platform.directory import models as directory_models  # noqa: F401
from app.platform.organization import models as organization_models  # noqa: F401
from app.platform.policies import models as policy_models  # noqa: F401
from app.platform.school_facts.application import SchoolFactChangeService
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
)
from app.platform.school_facts.ports import (
    FactVersionProjection,
    PreparedConfirmation,
    PreparedConfirmationResult,
    SchoolFactApplyResult,
)

NOW = datetime(2026, 8, 21, 8, tzinfo=UTC)


def _referred_table_names(table: Table) -> set[str]:
    return {
        element.target_fullname.rsplit(".", 1)[0].rsplit(".", 1)[-1]
        for constraint in table.foreign_key_constraints
        for element in constraint.elements
    }


def _local_reference_columns(table: Table, referred_table: str) -> tuple[str, ...]:
    columns = {
        element.parent.name
        for constraint in table.foreign_key_constraints
        if constraint.referred_table.name == referred_table
        for element in constraint.elements
        if element.parent.name != "school_id"
    }
    return tuple(sorted(columns))


def _has_same_school_reference(
    table: Table,
    referred_table: str,
    local_identity_column: str,
) -> bool:
    return any(
        {(element.parent.name, element.column.name) for element in constraint.elements}
        == {("school_id", "school_id"), (local_identity_column, "id")}
        for constraint in table.foreign_key_constraints
        if constraint.referred_table.name == referred_table
    )


def _organization_campus_relation() -> Table:
    matches = [
        table
        for table in Base.metadata.tables.values()
        if {"organization_units", "campus_locations"} <= _referred_table_names(table)
        and {"school_policies", "school_policy_versions"}.isdisjoint(_referred_table_names(table))
        and {"contact_value", "content_text"}.isdisjoint(table.c.keys())
    ]
    assert len(matches) == 1, (
        "必须有且只有一张通过稳定外键连接组织与校区的中性有效期关系表；"
        f"实际为 {[table.name for table in matches]}"
    )
    return matches[0]


def _policy_scope_relation() -> Table:
    candidates: list[Table] = []
    for table in Base.metadata.tables.values():
        targets = _referred_table_names(table)
        has_policy_target = bool({"school_policies", "school_policy_versions"} & targets)
        if has_policy_target and {"organization_units", "campus_locations"} <= targets:
            candidates.append(table)
    assert len(candidates) == 1, (
        "必须有且只有一张制度适用范围关系表，且通过外键连接制度、组织与校区；"
        f"实际为 {[table.name for table in candidates]}"
    )
    return candidates[0]


def _check_texts(table: Table) -> tuple[str, ...]:
    return tuple(
        " ".join(str(constraint.sqltext).lower().split())
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    )


def _assert_exactly_one_target_check(
    table: Table,
    organization_column: str,
    campus_column: str,
) -> None:
    matching = [
        text
        for text in _check_texts(table)
        if organization_column.lower() in text and campus_column.lower() in text
    ]
    assert matching, "制度适用范围必须有同时约束组织与校区目标的 exactly-one CHECK"
    normalized = " ".join(matching)
    explicit_xor = all(
        fragment in normalized
        for fragment in (
            f"{organization_column.lower()} is not null",
            f"{organization_column.lower()} is null",
            f"{campus_column.lower()} is not null",
            f"{campus_column.lower()} is null",
        )
    )
    counted_xor = "num_nonnulls" in normalized and "= 1" in normalized
    assert explicit_xor or counted_xor, normalized


class _ScalarResult:
    def __init__(
        self,
        value: object | None,
        values: tuple[object, ...] = (),
        rows: tuple[tuple[object, ...], ...] = (),
    ) -> None:
        self._value = value
        self._values = values
        self._rows = rows

    def __iter__(self) -> Iterator[tuple[object, ...]]:
        return iter(self._rows)

    def scalar_one_or_none(self) -> object | None:
        return self._value

    def scalars(self) -> tuple[object, ...]:
        return self._values


class _CampusKindSession:
    def __init__(self) -> None:
        self.statements: list[tuple[str, Mapping[str, object]]] = []
        self.location_kinds = {
            "location:main-campus": "campus",
            "location:teaching-building": "building",
        }

    def __enter__(self) -> _CampusKindSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _CampusKindSession:
        return self

    @staticmethod
    def _requires_campus(sql: str, params: Mapping[str, object]) -> bool:
        return "kind" in sql.lower() and ("campus" in sql.lower() or params.get("kind") == "campus")

    def execute(
        self,
        statement: object,
        params: Mapping[str, object] | None = None,
    ) -> _ScalarResult:
        params = params or {}
        sql = " ".join(str(statement).split())
        self.statements.append((sql, params))
        if "master_data_import_batches" in sql:
            return _ScalarResult(None)
        if "target_version" in sql or "SELECT version FROM" in sql:
            return _ScalarResult(None)
        if "campus_locations" not in sql:
            source_keys = params.get("source_keys")
            return _ScalarResult(
                1,
                tuple(source_keys) if isinstance(source_keys, (tuple, list)) else (),
            )
        source_key = str(params.get("source_key"))
        exists = source_key in self.location_kinds
        if self._requires_campus(sql, params):
            exists = self.location_kinds.get(source_key) == "campus"
        source_keys = params.get("source_keys")
        values = (
            tuple(
                key
                for key in source_keys
                if key in self.location_kinds
                and (not self._requires_campus(sql, params) or self.location_kinds[key] == "campus")
            )
            if isinstance(source_keys, (tuple, list))
            else ()
        )
        return _ScalarResult(1 if exists else None, values)


class _CampusKindFactory:
    def __init__(self) -> None:
        self.sessions: list[_CampusKindSession] = []

    def __call__(self) -> _CampusKindSession:
        session = _CampusKindSession()
        self.sessions.append(session)
        return session


@dataclass
class _FormalPreflightReader:
    persistence: SqlAlchemySchoolFactPersistence
    version_bulk_calls: int = 0
    reference_bulk_calls: int = 0

    def current_school_fact_version(self, school_id: SchoolId) -> int:
        del school_id
        return 4

    def find_fact_version(
        self,
        school_id: SchoolId,
        fact_kind: str,
        source_key: str,
    ) -> FactVersionProjection | None:
        del school_id, fact_kind, source_key
        raise AssertionError("正式预检必须使用 find_fact_versions_bulk")

    def find_fact_versions_bulk(
        self,
        school_id: SchoolId,
        targets: tuple[tuple[str, str], ...],
    ) -> dict[tuple[str, str], FactVersionProjection]:
        del school_id, targets
        self.version_bulk_calls += 1
        return {}

    def unresolved_reference_codes(
        self,
        school_id: SchoolId,
        change: CanonicalFactChangeV1,
    ) -> tuple[str, ...]:
        del school_id, change
        raise AssertionError("正式预检必须使用 unresolved_reference_codes_bulk")

    def unresolved_reference_codes_bulk(
        self,
        school_id: SchoolId,
        changes: tuple[CanonicalFactChangeV1, ...],
    ) -> dict[tuple[str, str], tuple[str, ...]]:
        self.reference_bulk_calls += 1
        return self.persistence.unresolved_reference_codes_bulk(school_id, changes)

    def find_residence_student_source_keys_bulk(
        self,
        school_id: SchoolId,
        residence_source_keys: tuple[str, ...],
    ) -> dict[str, str]:
        return self.persistence.find_residence_student_source_keys_bulk(
            school_id,
            residence_source_keys,
        )

    def find_applied_plan(
        self,
        school_id: SchoolId,
        source_sha256: str,
        plan_sha256: str,
    ) -> SchoolFactApplyResult | None:
        del school_id, source_sha256, plan_sha256
        return None


class _WriteTrap:
    def prepare(self, prepared: PreparedConfirmation) -> PreparedConfirmationResult:
        del prepared
        raise AssertionError("预检不得进入 prepare 写路径")

    def confirm(
        self,
        *,
        credential_sha256: str,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_sha256: str,
        now: datetime,
    ) -> SchoolFactApplyResult:
        del credential_sha256, change_set, context, session_sha256, now
        raise AssertionError("预检不得进入 confirm 写路径")


def _service(
    factory: _CampusKindFactory,
) -> tuple[SchoolFactChangeService, _FormalPreflightReader]:
    persistence = SqlAlchemySchoolFactPersistence(
        factory,  # type: ignore[arg-type]
        lambda _context: None,  # type: ignore[arg-type]
    )
    reader = _FormalPreflightReader(persistence)
    return SchoolFactChangeService(
        reader=reader,
        writer=_WriteTrap(),
    ), reader


FORMAL_CAMPUS_TARGET_CASES = (
    (
        "organization_campus_relationship",
        "campus_source_key",
        {
            "organization_source_key": "organization:schoolwide",
            "campus_source_key": "location:teaching-building",
        },
    ),
    (
        "student_school_relationship",
        "primary_campus_source_key",
        {
            "student_source_key": "student:001",
            "student_number": "SIM-001",
            "enrollment_status": "enrolled",
            "residence_mode": "day",
            "primary_campus_source_key": "location:teaching-building",
            "administrative_campus_source_key": "location:main-campus",
        },
    ),
    (
        "student_school_relationship",
        "administrative_campus_source_key",
        {
            "student_source_key": "student:001",
            "student_number": "SIM-002",
            "enrollment_status": "enrolled",
            "residence_mode": "day",
            "primary_campus_source_key": "location:main-campus",
            "administrative_campus_source_key": "location:teaching-building",
        },
    ),
    (
        "policy_scope",
        "campus_source_key",
        {
            "policy_source_key": "policy:late-arrival",
            "campus_source_key": "location:teaching-building",
        },
    ),
)


def _validated_change(
    fact_kind: str,
    content: Mapping[str, object],
) -> CanonicalFactChangeV1:
    try:
        return CanonicalFactChangeV1.model_validate(
            {
                "schema_version": 1,
                "operation": "upsert",
                "fact_kind": fact_kind,
                "source_key": f"{fact_kind}:target",
                "expected_version": 0,
                "effective_at": NOW,
                "origin": {
                    "sheet_code": "canonical_changes",
                    "row_number": 2,
                },
                "content": dict(content),
            }
        )
    except ValidationError as error:
        pytest.fail(
            "正式 CanonicalFactChangeV1 尚不能表达已冻结的校区目标语义；"
            f"fact_kind={fact_kind}: {error}"
        )


def _change_set(change: CanonicalFactChangeV1) -> CanonicalFactChangeSetV1:
    return CanonicalFactChangeSetV1(
        schema_version=1,
        source_sha256="c" * 64,
        base_version=4,
        changes=(change,),
    )


def _queried_campus_kind(factory: _CampusKindFactory) -> bool:
    return any(
        "campus_locations" in sql and _CampusKindSession._requires_campus(sql, params)  # noqa: SLF001
        for session in factory.sessions
        for sql, params in session.statements
    )


def _valid_campus_content(content: Mapping[str, object]) -> dict[str, object]:
    return {
        field_code: ("location:main-campus" if "campus_source_key" in field_code else value)
        for field_code, value in content.items()
    }


@pytest.mark.parametrize(
    ("fact_kind", "invalid_field_code", "content"),
    FORMAL_CAMPUS_TARGET_CASES,
)
def test_formal_preflight_rejects_non_campus_location_for_every_campus_target(
    fact_kind: str,
    invalid_field_code: str,
    content: Mapping[str, object],
) -> None:
    factory = _CampusKindFactory()
    change = _validated_change(fact_kind, content)
    service, reader = _service(factory)

    report = service.preflight(
        school_id=SchoolId(uuid4()),
        change_set=_change_set(change),
    )

    assert report.is_clean is False
    assert tuple((issue.column_code, issue.error_code) for issue in report.issues) == (
        (invalid_field_code, "fact.reference_not_found"),
    )
    assert reader.version_bulk_calls == 1
    assert reader.reference_bulk_calls == 1
    assert _queried_campus_kind(factory)


@pytest.mark.parametrize(
    ("fact_kind", "_invalid_field_code", "content"),
    FORMAL_CAMPUS_TARGET_CASES,
)
def test_formal_preflight_accepts_and_resolves_valid_campus_kind(
    fact_kind: str,
    _invalid_field_code: str,
    content: Mapping[str, object],
) -> None:
    factory = _CampusKindFactory()
    change = _validated_change(fact_kind, _valid_campus_content(content))
    service, reader = _service(factory)

    report = service.preflight(
        school_id=SchoolId(uuid4()),
        change_set=_change_set(change),
    )

    assert report.is_clean is True
    assert report.issues == ()
    assert reader.version_bulk_calls == 1
    assert reader.reference_bulk_calls == 1
    assert _queried_campus_kind(factory)


class _CoverageSession:
    def __init__(self) -> None:
        self.school_id = uuid4()
        self.campus_ids = (uuid4(), uuid4())
        self.coverage = {
            "organization:covered": (self.campus_ids[0],),
            "organization:no-relations": (),
        }
        self.statements: list[tuple[str, Mapping[str, object]]] = []

    def __enter__(self) -> _CoverageSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _CoverageSession:
        return self

    def execute(
        self,
        statement: object,
        params: Mapping[str, object] | None = None,
    ) -> _ScalarResult:
        params = params or {}
        statement_text = " ".join(str(statement).lower().split())
        self.statements.append((statement_text, params))
        organization = params.get("organization_source_key")
        reads_effective_relationship = all(
            marker in statement_text
            for marker in (
                "organization_units",
                "campus_locations",
                "valid_from",
                "valid_to",
                "school_id",
                "organization_source_key",
                "effective_at",
            )
        )
        has_required_filters = (
            params.get("school_id") == self.school_id
            and organization in self.coverage
            and params.get("effective_at") == NOW
        )
        values = (
            self.coverage[str(organization)]
            if reads_effective_relationship and has_required_filters
            else ()
        )
        return _ScalarResult(None, values)


class _CoverageFactory:
    def __init__(self) -> None:
        self.session = _CoverageSession()

    def __call__(self) -> _CoverageSession:
        return self.session


def test_organization_campus_coverage_reader_never_expands_zero_rows_to_all_campuses() -> None:
    factory = _CoverageFactory()
    persistence = SqlAlchemySchoolFactPersistence(
        factory,  # type: ignore[arg-type]
        lambda _context: None,  # type: ignore[arg-type]
    )
    reader = getattr(persistence, "find_organization_campus_ids", None)
    assert callable(reader), (
        "持久化只读端口必须提供窄查询 find_organization_campus_ids"
        "(school_id, organization_source_key, effective_at)"
    )

    school_id = factory.session.school_id
    covered = reader(school_id, "organization:covered", NOW)
    uncovered = reader(school_id, "organization:no-relations", NOW)

    assert len(factory.session.campus_ids) == 2
    assert covered == (factory.session.campus_ids[0],)
    assert uncovered == ()
    assert len(factory.session.statements) == 2
    for (statement, params), organization_source_key in zip(
        factory.session.statements,
        ("organization:covered", "organization:no-relations"),
        strict=True,
    ):
        assert "organization_units" in statement
        assert "campus_locations" in statement
        assert "valid_from" in statement
        assert "valid_to" in statement
        assert "school_id" in statement
        assert "organization_source_key" in statement
        assert "effective_at" in statement
        assert params["school_id"] == school_id
        assert params["organization_source_key"] == organization_source_key
        assert params["effective_at"] == NOW


def test_organization_campus_is_an_explicit_stable_identity_relation() -> None:
    relation = _organization_campus_relation()
    organization_columns = _local_reference_columns(relation, "organization_units")
    campus_columns = _local_reference_columns(relation, "campus_locations")

    assert len(organization_columns) == 1
    assert len(campus_columns) == 1
    assert {"valid_from", "valid_to"} <= set(relation.c.keys())
    assert all(not relation.c[name].nullable for name in organization_columns + campus_columns)
    assert _has_same_school_reference(relation, "organization_units", organization_columns[0])
    assert _has_same_school_reference(relation, "campus_locations", campus_columns[0])
    interval_checks = " ".join(_check_texts(relation))
    assert "valid_from" in interval_checks
    assert "valid_to" in interval_checks
    assert "valid_to is null" in interval_checks
    assert "valid_to > valid_from" in interval_checks
    assert {"display_name", "display_path"}.isdisjoint(relation.c.keys())


def test_schoolwide_organization_coverage_requires_explicit_non_null_campus_rows() -> None:
    relation = _organization_campus_relation()
    campus_columns = _local_reference_columns(relation, "campus_locations")

    assert len(campus_columns) == 1
    assert relation.c[campus_columns[0]].nullable is False


def test_day_students_still_have_primary_and_administrative_campus_identities() -> None:
    relationship = Base.metadata.tables["student_school_relationships"]
    campus_columns = _local_reference_columns(relationship, "campus_locations")

    assert len(campus_columns) == 2
    assert any("primary" in name for name in campus_columns)
    assert any("administrative" in name for name in campus_columns)
    assert all(not relationship.c[name].nullable for name in campus_columns)
    assert all(
        _has_same_school_reference(relationship, "campus_locations", column)
        for column in campus_columns
    ), "主要／行政校区都必须使用 (school_id, campus_id) 复合 FK，不能只校验 UUID 存在"

    campus_ids = {name: uuid4() for name in campus_columns}
    row = student_models.StudentSchoolRelationship(
        school_id=uuid4(),
        student_id=uuid4(),
        student_number="SIM-DAY-001",
        enrollment_status="enrolled",
        residence_mode="day",
        valid_from=datetime.now(UTC),
        source_id=uuid4(),
        **campus_ids,
    )
    assert row.residence_mode == "day"
    assert {getattr(row, name) for name in campus_columns} == set(campus_ids.values())


def test_staff_assignment_to_organization_to_campus_is_a_mechanical_fk_chain() -> None:
    assignments = Base.metadata.tables["staff_assignments"]
    relation = _organization_campus_relation()

    assert _local_reference_columns(assignments, "school_people") == ("person_id",)
    assert _local_reference_columns(assignments, "organization_units") == ("organization_unit_id",)
    assert len(_local_reference_columns(relation, "organization_units")) == 1
    assert len(_local_reference_columns(relation, "campus_locations")) == 1


def test_policy_scope_targets_exactly_one_campus_or_organization() -> None:
    relation = _policy_scope_relation()
    organization_columns = _local_reference_columns(relation, "organization_units")
    campus_columns = _local_reference_columns(relation, "campus_locations")
    policy_target_tables = tuple(
        table_name
        for table_name in ("school_policies", "school_policy_versions")
        if table_name in _referred_table_names(relation)
    )

    assert len(organization_columns) == 1
    assert len(campus_columns) == 1
    assert len(policy_target_tables) == 1, "制度适用范围必须以一条稳定 FK 指向制度或制度版本"
    assert relation.c[organization_columns[0]].nullable is True
    assert relation.c[campus_columns[0]].nullable is True
    policy_target = policy_target_tables[0]
    policy_columns = _local_reference_columns(relation, policy_target)
    assert len(policy_columns) == 1
    assert _has_same_school_reference(relation, policy_target, policy_columns[0])
    assert _has_same_school_reference(relation, "organization_units", organization_columns[0])
    assert _has_same_school_reference(relation, "campus_locations", campus_columns[0])
    assert {"valid_from", "valid_to"} <= set(relation.c.keys())
    interval_checks = " ".join(_check_texts(relation))
    assert "valid_from" in interval_checks
    assert "valid_to" in interval_checks
    assert "valid_to is null" in interval_checks
    assert "valid_to > valid_from" in interval_checks
    _assert_exactly_one_target_check(relation, organization_columns[0], campus_columns[0])


def test_policy_publisher_and_scope_are_independent_facts_that_may_differ() -> None:
    policy_versions = Base.metadata.tables["school_policy_versions"]
    scope_relation = _policy_scope_relation()
    scope_organization_columns = _local_reference_columns(scope_relation, "organization_units")

    assert "publishing_organization_unit_id" in policy_versions.c
    assert len(scope_organization_columns) == 1
    assert "publishing_organization_unit_id" not in scope_relation.c
    assert scope_organization_columns[0] not in policy_versions.c


def test_scenario_one_has_only_the_shared_facts_needed_for_later_authorization() -> None:
    assignments = Base.metadata.tables["staff_assignments"]
    student_relationships = Base.metadata.tables["student_school_relationships"]
    dorm_rooms = Base.metadata.tables["dorm_room_profile_versions"]
    organization_campuses = _organization_campus_relation()

    assert _local_reference_columns(assignments, "school_people") == ("person_id",)
    assert _local_reference_columns(assignments, "organization_units") == ("organization_unit_id",)
    assert len(_local_reference_columns(organization_campuses, "campus_locations")) == 1
    assert len(_local_reference_columns(student_relationships, "campus_locations")) == 2
    assert _local_reference_columns(dorm_rooms, "campus_locations") == ("room_location_id",)


def test_scenario_three_can_scope_a_policy_to_south_campus_by_stable_identity() -> None:
    scope_relation = _policy_scope_relation()
    campus_columns = _local_reference_columns(scope_relation, "campus_locations")

    assert len(campus_columns) == 1
    assert "display_name" not in scope_relation.c
