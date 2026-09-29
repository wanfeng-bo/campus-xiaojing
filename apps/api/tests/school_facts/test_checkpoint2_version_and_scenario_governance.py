from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.platform.contracts import SchoolId

REPO_ROOT = Path(__file__).resolve().parents[4]
PROTOCOL_PATH = REPO_ROOT / "docs/engineering/学校综合智能体编程协议.md"

USER_SCENARIOS = (
    "城南分校王校长管理城南分校的教师、学生和宿舍。",
    "城南分校单独调休，主校区不调休。",
    "这条晚归处理规定只在城南分校执行。",
    "李老师主校区任教，同时在城南分校兼两个班的课。",
    "张三是城南分校走读学生，家住城南，从不住校。",
)

CURRENT_STAGE_JUDGMENT = (
    "当前判定：第1、3条属本轮修复范围；第2、4、5分别依赖校历适用范围、"
    "教师—班级事实、家庭地址，当前阶段不做，不得扩范围。"
)


class _ScalarSequence:
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = values

    def __iter__(self) -> Iterator[object]:
        return iter(self._values)

    def all(self) -> list[object]:
        return list(self._values)


class _QueryResult:
    def __init__(
        self,
        *,
        scalar: object | None = None,
        rows: tuple[tuple[object, ...], ...] = (),
    ) -> None:
        self._scalar = scalar
        self._rows = rows

    def __iter__(self) -> Iterator[tuple[object, ...]]:
        return iter(self._rows)

    def scalar_one_or_none(self) -> object | None:
        return self._scalar

    def scalar_one(self) -> object | None:
        return self._scalar

    def one_or_none(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None

    def scalars(self) -> _ScalarSequence:
        return _ScalarSequence(tuple(row[0] for row in self._rows))


class _RecordingSession:
    def __init__(
        self,
        result_for_sql: Callable[[str, Mapping[str, object]], _QueryResult],
    ) -> None:
        self._result_for_sql = result_for_sql
        self.statements: list[str] = []

    def __enter__(self) -> _RecordingSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _RecordingSession:
        return self

    def execute(
        self,
        statement: object,
        params: Mapping[str, object],
    ) -> _QueryResult:
        sql = " ".join(str(statement).split())
        self.statements.append(sql)
        return self._result_for_sql(sql, params)


class _SessionFactory:
    def __init__(
        self,
        result_for_sql: Callable[[str, Mapping[str, object]], _QueryResult],
    ) -> None:
        self._result_for_sql = result_for_sql
        self.sessions: list[_RecordingSession] = []

    def __call__(self) -> _RecordingSession:
        session = _RecordingSession(self._result_for_sql)
        self.sessions.append(session)
        return session

    @property
    def statements(self) -> tuple[str, ...]:
        return tuple(statement for session in self.sessions for statement in session.statements)


def _persistence(factory: _SessionFactory) -> SqlAlchemySchoolFactPersistence:
    return SqlAlchemySchoolFactPersistence(
        factory,  # type: ignore[arg-type]
        lambda _context: None,  # type: ignore[arg-type]
    )


def _version_factory() -> tuple[_SessionFactory, object, int, int]:
    target_id = uuid4()
    lineage_target_version = 17
    physical_row_version = 901

    def result_for_sql(sql: str, params: Mapping[str, object]) -> _QueryResult:
        source_keys = params.get("source_keys")
        source_key = (
            source_keys[0]
            if isinstance(source_keys, (tuple, list)) and source_keys
            else params.get("source_key")
        )
        if "school_fact_change_lineage" in sql:
            if "GROUP BY fact_source_key" in sql:
                return _QueryResult(rows=((source_key, lineage_target_version),))
            if "SELECT target_id, target_version" in sql:
                return _QueryResult(rows=((target_id, lineage_target_version),))
            return _QueryResult(scalar=lineage_target_version)
        if "ANY(:source_keys)" in sql:
            return _QueryResult(rows=((source_key, physical_row_version),))
        if "SELECT id, version" in sql:
            return _QueryResult(rows=((target_id, physical_row_version),))
        return _QueryResult(scalar=physical_row_version)

    return (
        _SessionFactory(result_for_sql),
        target_id,
        lineage_target_version,
        physical_row_version,
    )


NON_ROOT_FACT_KINDS = (
    "organization_campus_relationship",
    "policy_scope",
    "assignment",
    "login_mapping",
    "student_school_relationship",
    "location_alias",
    "dorm_room",
    "dorm_bed",
    "student_bed_residence",
    "person_contact",
    "student_contact",
    "official_contact",
    "policy",
    "policy_version",
)

ROOT_FACT_TABLES = (
    ("school", "schools"),
    ("person", "school_people"),
    ("student", "students"),
    ("organization", "organization_units"),
    ("location", "campus_locations"),
)


@pytest.mark.parametrize(
    "fact_kind",
    NON_ROOT_FACT_KINDS,
)
def test_non_root_current_version_comes_only_from_lineage_target_version(
    fact_kind: str,
) -> None:
    factory, _target_id, lineage_target_version, _physical_row_version = _version_factory()

    projection = _persistence(factory).find_fact_version(
        SchoolId(uuid4()), fact_kind, f"{fact_kind}:stable"
    )

    assert projection is not None
    assert projection.version == lineage_target_version
    assert len(factory.statements) == 1
    assert "school_fact_change_lineage" in factory.statements[0]
    assert "target_version" in factory.statements[0]


@pytest.mark.parametrize("fact_kind", NON_ROOT_FACT_KINDS)
def test_non_root_bulk_preflight_version_comes_only_from_lineage_target_version(
    fact_kind: str,
) -> None:
    factory, _target_id, lineage_target_version, _physical_row_version = _version_factory()
    source_key = f"{fact_kind}:stable"

    projections = _persistence(factory).find_fact_versions_bulk(
        SchoolId(uuid4()), ((fact_kind, source_key),)
    )

    assert projections[(fact_kind, source_key)].version == lineage_target_version
    assert len(factory.statements) == 1
    assert "school_fact_change_lineage" in factory.statements[0]
    assert "target_version" in factory.statements[0]


@pytest.mark.parametrize("fact_kind", NON_ROOT_FACT_KINDS)
def test_non_root_confirm_lock_uses_only_lineage_target_version(
    fact_kind: str,
) -> None:
    factory, target_id, lineage_target_version, _physical_row_version = _version_factory()
    persistence = _persistence(factory)
    session = factory()

    locked = persistence._lock_target(  # noqa: SLF001 - confirm version-path RED
        cast(Session, session),
        uuid4(),
        fact_kind,
        f"{fact_kind}:stable",
    )

    assert locked == (target_id, lineage_target_version)
    assert len(factory.statements) == 1
    assert "school_fact_change_lineage" in factory.statements[0]
    assert "target_version" in factory.statements[0]


@pytest.mark.parametrize(
    ("fact_kind", "root_table"),
    ROOT_FACT_TABLES,
)
def test_root_entity_current_version_is_its_row_optimistic_version(
    fact_kind: str,
    root_table: str,
) -> None:
    factory, _target_id, _lineage_target_version, root_row_version = _version_factory()

    projection = _persistence(factory).find_fact_version(
        SchoolId(uuid4()), fact_kind, f"{fact_kind}:stable"
    )

    assert projection is not None
    assert projection.version == root_row_version
    assert len(factory.statements) == 1
    assert f"SELECT version FROM {root_table}" in factory.statements[0]
    assert "school_fact_change_lineage" not in factory.statements[0]


@pytest.mark.parametrize(("fact_kind", "root_table"), ROOT_FACT_TABLES)
def test_root_bulk_preflight_version_is_its_row_optimistic_version(
    fact_kind: str,
    root_table: str,
) -> None:
    factory, _target_id, _lineage_target_version, root_row_version = _version_factory()
    source_key = f"{fact_kind}:stable"

    projections = _persistence(factory).find_fact_versions_bulk(
        SchoolId(uuid4()), ((fact_kind, source_key),)
    )

    assert projections[(fact_kind, source_key)].version == root_row_version
    assert len(factory.statements) == 1
    assert f"FROM {root_table}" in factory.statements[0]
    assert "school_fact_change_lineage" not in factory.statements[0]


@pytest.mark.parametrize(("fact_kind", "root_table"), ROOT_FACT_TABLES)
def test_root_confirm_lock_uses_its_row_optimistic_version(
    fact_kind: str,
    root_table: str,
) -> None:
    factory, target_id, _lineage_target_version, root_row_version = _version_factory()
    persistence = _persistence(factory)
    session = factory()

    locked = persistence._lock_target(  # noqa: SLF001 - confirm version-path RED
        cast(Session, session),
        uuid4(),
        fact_kind,
        f"{fact_kind}:stable",
    )

    assert locked == (target_id, root_row_version)
    assert len(factory.statements) == 1
    assert f"FROM {root_table}" in factory.statements[0]
    assert "school_fact_change_lineage" not in factory.statements[0]


def test_user_scenario_collection_and_order_are_an_exact_governance_gate() -> None:
    lines = PROTOCOL_PATH.read_text(encoding="utf-8").splitlines()
    heading_index = lines.index("首批场景如下，用户已给出，不得改写、不得自行增删：")
    judgment_index = lines.index(CURRENT_STAGE_JUDGMENT)
    actual = tuple(
        line.removeprefix("- ")
        for line in lines[heading_index + 1 : judgment_index]
        if line.startswith("- ")
    )

    assert actual == USER_SCENARIOS


def test_scenarios_two_four_and_five_remain_exactly_out_of_scope_for_this_stage() -> None:
    source = PROTOCOL_PATH.read_text(encoding="utf-8")

    assert CURRENT_STAGE_JUDGMENT in source
    assert "第2、4、5" in CURRENT_STAGE_JUDGMENT
    assert "当前阶段不做，不得扩范围" in CURRENT_STAGE_JUDGMENT
