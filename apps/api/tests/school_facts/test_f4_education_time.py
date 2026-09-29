from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from importlib import import_module
from importlib.util import find_spec

import pytest


def api():
    name = "app.domains.students.education_time"
    assert find_spec(name) is not None, "F4 education time domain is not implemented"
    return import_module(name)


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def rule(**changes):
    values = dict(
        rule_id="school-calendar",
        version=1,
        start_cycle=2026,
        end_cycle=None,
        month=9,
        day=1,
        timezone="Asia/Shanghai",
        grade_ids=("junior-1", "junior-2", "junior-3"),
        recorded_at=instant("2026-01-01T00:00:00+08:00"),
    )
    values.update(changes)
    return api().CalendarRule(**values)


def anchor(**changes):
    values = dict(
        anchor_id="fixed-class-origin",
        version=1,
        effective_from=instant("2026-09-01T00:00:00+08:00"),
        grade_id="junior-1",
        recorded_at=instant("2026-08-01T00:00:00+08:00"),
    )
    values.update(changes)
    return api().GradeAnchor(**values)


def resolve(value: str, *, rules=None, anchors=None, **kwargs):
    return api().resolve_grade(
        at=instant(value),
        rules=tuple(rules if rules is not None else [rule()]),
        anchors=tuple(anchors if anchors is not None else [anchor()]),
        **kwargs,
    )


def test_school_midnight_is_half_open_in_utc_and_requires_no_year_rows():
    before = resolve("2027-08-31T15:59:59+00:00")
    after = resolve("2027-08-31T16:00:00+00:00")
    assert (before.cycle, before.grade_id) == (2026, "junior-1")
    assert before.next_boundary == instant("2027-09-01T00:00:00+08:00")
    assert (after.cycle, after.grade_id) == (2027, "junior-2")


def test_restart_after_two_years_and_repeat_reads_do_not_mutate_origin():
    origin = anchor()
    for _ in range(2):
        result = resolve("2028-09-03T10:00:00+08:00", anchors=[origin])
        assert (result.status, result.grade_id) == ("resolved", "junior-3")
    assert origin.grade_id == "junior-1"


def test_school_configuration_can_use_four_years_and_april_boundary():
    custom = rule(month=4, day=10, grade_ids=("A", "B", "C", "D"))
    origin = anchor(effective_from=instant("2026-04-10T00:00:00+08:00"), grade_id="A")
    result = resolve("2029-04-10T00:00:00+08:00", rules=[custom], anchors=[origin])
    assert (result.status, result.cycle, result.grade_id) == ("resolved", 2029, "D")


def test_terminal_grade_does_not_graduate_cross_stage_or_repeat():
    result = resolve("2029-09-01T00:00:00+08:00")
    assert (result.status, result.grade_id, result.last_known_grade_id) == (
        "unknown",
        None,
        "junior-3",
    )
    assert result.reason == "stage_exhausted"


def test_next_cycle_date_change_does_not_advance_twice_or_backwards():
    rules = [rule(end_cycle=2027), rule(version=2, start_cycle=2027, month=10)]
    before = resolve("2027-09-15T12:00:00+08:00", rules=rules)
    after = resolve("2027-10-01T00:00:00+08:00", rules=rules)
    later = resolve("2028-10-01T00:00:00+08:00", rules=rules)
    assert before.grade_id == "junior-1"
    assert before.next_boundary == instant("2027-10-01T00:00:00+08:00")
    assert after.grade_id == "junior-2"
    assert later.grade_id == "junior-3"
    assert "rule:school-calendar:1" in later.dependencies


def test_ended_rule_is_not_frozen_and_gap_cannot_be_skipped():
    ended = rule(end_cycle=2027)
    assert resolve("2027-09-01T00:00:00+08:00", rules=[ended]).status == "unknown"
    result = resolve(
        "2028-09-01T00:00:00+08:00",
        rules=[ended, rule(version=2, start_cycle=2028)],
    )
    assert result.status == "unknown"
    assert result.reason == "rule_gap"


def test_conflicting_rule_or_same_time_exception_is_not_last_writer_wins():
    assert resolve("2027-09-01T00:00:00+08:00", rules=[rule(), rule(version=2)]).status == (
        "conflict"
    )
    assert (
        resolve(
            "2027-09-01T00:00:00+08:00",
            anchors=[anchor(), anchor(anchor_id="other", grade_id="junior-2")],
        ).status
        == "conflict"
    )


def test_formal_retention_is_new_origin_and_then_progresses_normally():
    retained = anchor(
        anchor_id="retention",
        effective_from=instant("2027-10-10T00:00:00+08:00"),
    )
    before = resolve("2027-10-09T12:00:00+08:00", anchors=[anchor(), retained])
    during = resolve("2027-10-10T00:00:00+08:00", anchors=[anchor(), retained])
    later = resolve("2028-09-01T00:00:00+08:00", anchors=[anchor(), retained])
    assert before.grade_id == "junior-2"
    assert before.next_boundary == retained.effective_from
    assert during.grade_id == "junior-1"
    assert later.grade_id == "junior-2"


def test_cancelled_future_origin_is_not_in_current_future_view_but_audit_preserves_it():
    cancelled = anchor(
        anchor_id="future",
        effective_from=instant("2027-10-10T00:00:00+08:00"),
        recorded_at=instant("2027-08-01T00:00:00+08:00"),
        superseded_at=instant("2027-09-10T00:00:00+08:00"),
    )
    now = resolve("2027-11-01T00:00:00+08:00", anchors=[anchor(), cancelled])
    then = resolve(
        "2027-11-01T00:00:00+08:00",
        anchors=[anchor(), cancelled],
        known_at=instant("2027-09-01T00:00:00+08:00"),
    )
    assert now.grade_id == "junior-2"
    assert then.grade_id == "junior-1"


def test_historical_correction_keeps_fact_time_separate_from_recorded_time():
    correction_time = instant("2027-11-05T00:00:00+08:00")
    old = anchor(superseded_at=correction_time)
    corrected = anchor(version=2, grade_id="junior-2", recorded_at=correction_time)
    current_history = resolve("2026-10-20T00:00:00+08:00", anchors=[old, corrected])
    old_audit = resolve(
        "2026-10-20T00:00:00+08:00",
        anchors=[old, corrected],
        known_at=instant("2027-11-04T23:59:59+08:00"),
    )
    at_correction = resolve(
        "2026-10-20T00:00:00+08:00",
        anchors=[old, corrected],
        known_at=correction_time,
    )
    assert current_history.grade_id == at_correction.grade_id == "junior-2"
    assert old_audit.grade_id == "junior-1"


def test_cancelled_replacement_needs_explicit_causal_restoration_of_old_rule():
    cancelled_at = instant("2027-08-15T00:00:00+08:00")
    old = rule(end_cycle=2027, superseded_at=cancelled_at)
    cancelled = rule(version=2, start_cycle=2027, month=10, superseded_at=cancelled_at)
    restored = rule(version=3, recorded_at=cancelled_at)
    result = resolve("2027-09-02T00:00:00+08:00", rules=[old, cancelled, restored])
    assert result.grade_id == "junior-2"
    independent_end = replace(restored, end_cycle=2027)
    assert (
        resolve("2027-09-02T00:00:00+08:00", rules=[old, cancelled, independent_end]).status
        == "unknown"
    )


def test_unknown_personal_arrangement_does_not_copy_retained_class_grade():
    unknown = anchor(
        anchor_id="suspension",
        effective_from=instant("2027-10-01T00:00:00+08:00"),
        grade_id=None,
    )
    result = resolve("2028-10-01T00:00:00+08:00", anchors=[anchor(), unknown])
    assert (result.status, result.grade_id, result.reason) == (
        "unknown",
        None,
        "explicit_unknown_origin",
    )


def test_naive_times_and_invalid_calendar_configuration_are_rejected():
    with pytest.raises(ValueError, match="timezone"):
        api().resolve_grade(at=datetime(2027, 9, 1), rules=(rule(),), anchors=(anchor(),))
    with pytest.raises(ValueError, match="timezone"):
        resolve("2027-09-01T00:00:00+08:00", known_at=datetime(2027, 1, 1))
    with pytest.raises(ValueError):
        rule(month=2, day=29)
    with pytest.raises(ValueError):
        rule(grade_ids=("same", "same"))
    with pytest.raises(ValueError):
        rule(end_cycle=2026)


def test_physical_calendar_cutoff_intersects_explicit_cycles_at_school_boundary():
    clip = api().clip_calendar_rule
    assert (
        clip(rule(end_cycle=2030), valid_to=instant("2027-08-31T16:00:00+00:00")).end_cycle == 2027
    )
    assert (
        clip(rule(end_cycle=2027), valid_to=instant("2028-01-01T00:00:00+08:00")).end_cycle == 2027
    )
    assert clip(rule(), valid_to=instant("2026-08-01T00:00:00+08:00")) is None
    assert clip(rule(), valid_to=instant("2026-10-01T00:00:00+08:00")).end_cycle == 2027
