from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.domains.students.education_versions import EducationSlice, revise_timeline
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeV3

SEP = datetime(2026, 9, 1, tzinfo=UTC)
OCT = datetime(2026, 10, 1, tzinfo=UTC)
NOV = datetime(2026, 11, 1, tzinfo=UTC)
DEC = datetime(2026, 12, 1, tzinfo=UTC)


def original_segment(start, end, teacher):
    from app.platform.school_facts.ports import SchoolFactOwnOriginalSegment

    return SchoolFactOwnOriginalSegment(
        start,
        end,
        {
            "assignment_source_key": teacher,
            "offering_source_key": "offering:original",
            "teaching_role": "lead",
        },
    )


def test_original_revision_difference_omits_preserved_old_teacher_and_later_arrangement():
    before = (
        original_segment(SEP, NOV, "assignment:li"),
        original_segment(NOV, None, "assignment:other"),
    )
    after = (
        original_segment(SEP, OCT, "assignment:li"),
        original_segment(OCT, NOV, "assignment:chen"),
        original_segment(NOV, None, "assignment:other"),
    )
    old, new = SqlAlchemySchoolFactPersistence._original_segment_difference(before, after)
    assert old == (original_segment(OCT, NOV, "assignment:li"),)
    assert new == (original_segment(OCT, NOV, "assignment:chen"),)


@pytest.mark.parametrize("after", [(), (original_segment(SEP, OCT, "assignment:li"),)])
def test_original_revision_difference_preserves_end_and_cancellation_intervals(after):
    before = (original_segment(SEP, None, "assignment:li"),)
    old, new = SqlAlchemySchoolFactPersistence._original_segment_difference(before, after)
    assert new == ()
    assert old == (original_segment(OCT if after else SEP, None, "assignment:li"),)


def test_original_revision_difference_normalizes_unchanged_split_segments():
    before = (original_segment(SEP, None, "assignment:li"),)
    after = (
        original_segment(SEP, OCT, "assignment:li"),
        original_segment(OCT, None, "assignment:li"),
    )
    assert SqlAlchemySchoolFactPersistence._original_segment_difference(before, after) == ((), ())


def test_original_version_sql_selects_fields_and_exact_batch_revision_without_time_guess():
    from sqlalchemy.dialects import postgresql

    statements = []
    school, target, batch = uuid4(), uuid4(), uuid4()

    class Session:
        def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(
                all=lambda: [(SEP, None, {"assignment_source_key": "assignment:chen"})]
            )

    actual = SqlAlchemySchoolFactPersistence._original_version_segments(
        Session(),
        school,
        target,
        batch,
        2,
        ("assignment_source_key",),
    )
    assert actual[0].content == {"assignment_source_key": "assignment:chen"}
    sql = str(
        statements[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "school_education_versions.revision = 2" in sql
    assert str(batch) in sql and str(target) in sql and str(school) in sql
    assert "school_education_versions.import_batch_id" in sql
    projection = sql.split("FROM")[0]
    assert "jsonb_build_object" in projection
    assert "SELECT school_education_versions.payload" not in projection
    assert "superseded_at IS NULL" not in sql and "confirmed_at" not in sql


@pytest.mark.parametrize("broken", [None, "missing_previous", "missing_after", "wrong_owner"])
def test_original_receipt_persistence_binds_owner_batch_and_prior_revision(monkeypatch, broken):
    from contextlib import contextmanager

    from sqlalchemy.dialects import postgresql

    from app.platform.school_facts.ports import (
        SchoolFactApplyResult,
        SchoolFactOwnResult,
        SchoolFactOwnResultTarget,
    )
    from tests.school_facts.test_sf5b_writer_executor import _context

    context = _context()
    current_id, batch_id, previous_batch = uuid4(), uuid4(), uuid4()
    target = SchoolFactOwnResultTarget("teaching_assignment", "teaching:a", "upsert", 2)
    receipt = SchoolFactOwnResult(
        "d" * 64, SEP, SchoolFactApplyResult(batch_id, 9, 1, True), (target,)
    )
    statements = []
    projected = []
    original_rows = [] if broken == "wrong_owner" else [(current_id, 2, batch_id, "upsert")]
    prior_rows = [] if broken == "missing_previous" else [(current_id, 1, previous_batch, "upsert")]
    results = iter((original_rows, prior_rows))

    class Session:
        def execute(self, statement):
            statements.append(
                str(
                    statement.compile(
                        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                    )
                )
            )
            rows = next(results)
            return SimpleNamespace(all=lambda: rows)

    @contextmanager
    def read_session():
        yield Session()

    persistence = object.__new__(SqlAlchemySchoolFactPersistence)
    persistence._request_context = context
    monkeypatch.setattr(persistence, "_request_bound_session", read_session)

    def version_rows(session, school, fact_id, batch, revision, fields):
        projected.append((school, fact_id, batch, revision, fields))
        if revision == 2:
            return (
                ()
                if broken == "missing_after"
                else (
                    original_segment(SEP, OCT, "assignment:li"),
                    original_segment(OCT, None, "assignment:chen"),
                )
            )
        return (original_segment(SEP, None, "assignment:li"),)

    monkeypatch.setattr(persistence, "_original_version_segments", version_rows)
    fields = ("assignment_source_key", "offering_source_key", "teaching_role")
    if broken is not None:
        with pytest.raises(RuntimeError):
            persistence.read_own_original_target(context.actor, receipt, target, fields)
    else:
        value = persistence.read_own_original_target(context.actor, receipt, target, fields)
        assert value.segments == (original_segment(OCT, None, "assignment:chen"),)
        assert value.before_segments == (original_segment(OCT, None, "assignment:li"),)
        assert value.field_changes
        assert [(r[2], r[3]) for r in projected] == [(batch_id, 2), (previous_batch, 1)]
    exact = statements[0]
    assert str(batch_id) in exact and "target_version = 2" in exact
    assert "staged_by_user_id" in exact and "confirmed_by_person_id" in exact
    assert "result_locator_sha256" in exact and "teaching:a" in exact
    assert "status = 'applied'" in exact
    if len(statements) == 2:
        assert "target_version < 2" in statements[1]
        assert "target_version DESC" in statements[1]
    if broken == "wrong_owner":
        assert not projected


def test_read_only_preflight_rejects_partial_class_closure_and_accepts_complete_batch():
    rows = [
        (
            "education_class",
            "class:test",
            SimpleNamespace(valid_from=SEP, valid_to=None, payload={"display_name": "合成班"}),
        ),
        (
            "class_position_definition",
            "position:test",
            SimpleNamespace(
                valid_from=SEP,
                valid_to=None,
                payload={
                    "class_source_key": "class:test",
                    "display_name": "班长",
                    "allow_multiple": False,
                    "allow_concurrent": False,
                    "max_holders": 1,
                },
            ),
        ),
    ]

    class ReadOnlySession:
        def execute(self, statement):
            assert statement.is_select
            return SimpleNamespace(all=lambda: rows)

    def end(kind, key):
        return CanonicalFactChangeV3.model_validate(
            {
                "schema_version": 3,
                "origin": {"sheet_code": "education", "row_number": 1},
                "fact_kind": kind,
                "source_key": key,
                "operation": "end",
                "expected_version": 1,
                "effective_at": OCT,
                "content": None,
            }
        )

    close = end("education_class", "class:test")
    with pytest.raises(SchoolFactChangeRejected, match="关联"):
        SqlAlchemySchoolFactPersistence._validate_education_state(
            ReadOnlySession(), uuid4(), proposed_changes=(close,)
        )
    SqlAlchemySchoolFactPersistence._validate_education_state(
        ReadOnlySession(),
        uuid4(),
        proposed_changes=(close, end("class_position_definition", "position:test")),
    )
    assert all(row.valid_to is None for _, _, row in rows)


def test_backdated_correction_preserves_unaffected_history_and_later_change():
    rows = (
        EducationSlice(SEP, NOV, {"display_name": "旧"}),
        EducationSlice(NOV, None, {"display_name": "后来"}),
    )
    result = revise_timeline(
        rows, operation="correct", at=OCT, until=NOV, payload={"display_name": "补正"}, now=DEC
    )
    assert [(x.start, x.end, x.payload["display_name"]) for x in result] == [
        (SEP, OCT, "旧"),
        (OCT, NOV, "补正"),
        (NOV, None, "后来"),
    ]


def test_ordinary_upsert_does_not_silently_rewrite_later_arrangements():
    rows = (EducationSlice(SEP, NOV, {}), EducationSlice(NOV, None, {}))
    with pytest.raises(ValueError):
        revise_timeline(rows, operation="upsert", at=OCT, until=None, payload={}, now=DEC)


def test_cancellation_removes_future_fact_but_never_deletes_history():
    rows = (EducationSlice(NOV, None, {"display_name": "未来"}),)
    assert (
        revise_timeline(rows, operation="cancel", at=NOV, until=None, payload=None, now=OCT) == ()
    )
    with pytest.raises(ValueError):
        revise_timeline(rows, operation="cancel", at=NOV, until=None, payload=None, now=DEC)


def test_end_before_later_version_rejects_unreviewed_impact():
    with pytest.raises(ValueError):
        revise_timeline(
            (EducationSlice(NOV, None, {}),),
            operation="end",
            at=OCT,
            until=None,
            payload=None,
            now=DEC,
        )
