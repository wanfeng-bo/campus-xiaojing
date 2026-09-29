"""Owned PostgreSQL: exact middle cancellation through formal maintenance and sole Writer."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.composition.school_fact_education_maintenance import snapshot_at
from app.domains.students.teaching_change_planner import cancel_teaching_future
from tests.school_facts.test_f5_temporal_linkage_postgres import (
    apply,
    change,
    only_active,
    read_rows,
)
from tests.school_facts.test_f5_writer_reader_postgres import writer as writer

pytestmark = pytest.mark.skipif(not os.environ.get("F5_TEACHING_PG"), reason="explicit owned only")
SQL = text(
    "SELECT app.school_fact_f5_cancellation_proof(:school,:kind,:source,:fields,:start,:version)"
)


@pytest.mark.parametrize("independent_end", [False, True])
def test_selected_middle_cancellation_preserves_C_and_independent_A_end(writer, independent_end):
    now = datetime.now(UTC)
    start = now - timedelta(days=1)
    b = now + timedelta(days=2)
    c = now + timedelta(days=4)
    end = now + timedelta(days=3) if independent_end else None
    key = "cancel39:" + uuid4().hex
    acontent = {"display_name": "A", **({"valid_to": end} if end else {})}
    apply(writer, [change("teaching_course", key, start, acontent)])
    apply(writer, [change("teaching_course", key, b, {"display_name": "B"}, expected=1)])
    apply(writer, [change("teaching_course", key, c, {"display_name": "C"}, expected=2)])
    params = dict(
        school=writer[1].school_id,
        kind="teaching_course",
        source=key,
        fields=["display_name", "valid_to"],
        start=b,
        version=3,
    )
    with writer[0]["reader"].connect() as conn:
        evidence = conn.scalar(SQL, params)["records"][0]
    assert evidence["introduction_version"] == 2
    row = read_rows(writer, "teaching_course", [key], now)[0]
    command = cancel_teaching_future(
        change("teaching_course", key, b, operation="cancel", expected=3),
        snapshot_at(row, kind="teaching_course", at=now),
        proof=evidence,
        now=now,
    )
    result = apply(writer, [command])
    assert only_active(writer, "teaching_course", [key], b)[key]["display_name"] == "A"
    assert only_active(writer, "teaching_course", [key], c)[key]["display_name"] == "C"
    if end:
        assert only_active(writer, "teaching_course", [key], end) == {}
    for role in ("runtime", "writer"):
        with writer[0][role].connect() as conn, pytest.raises(DBAPIError):
            conn.scalar(SQL, params)
    with writer[0]["reader"].connect() as conn, pytest.raises(DBAPIError):
        conn.scalar(SQL, params)  # preview root version is now stale
    print("F5_CANCEL39_COMMIT", result.batch_id, "independent_end", independent_end)


def cancel_selected(writer, key, boundary, version, now):
    with writer[0]["reader"].connect() as conn:
        evidence = conn.scalar(
            SQL,
            dict(
                school=writer[1].school_id,
                kind="teaching_course",
                source=key,
                fields=["display_name", "valid_to"],
                start=boundary,
                version=version,
            ),
        )["records"][0]
    command = cancel_teaching_future(
        change("teaching_course", key, boundary, operation="cancel", expected=version),
        snapshot_at(
            read_rows(writer, "teaching_course", [key], now)[0], kind="teaching_course", at=now
        ),
        proof=evidence,
        now=now,
    )
    return apply(writer, [command])


@pytest.mark.parametrize(
    "later", ["history", "end_C", "end_B", "gap_B", "cancel_C", "reconfirm_B", "conflict_B"]
)
def test_cancellation_composes_with_later_independent_interval_changes(writer, later):
    from app.domains.students.teaching_change_planner import plan_teaching_change

    now = datetime.now(UTC)
    start, b, c = now - timedelta(days=4), now + timedelta(days=2), now + timedelta(days=4)
    key = "cancel41:" + uuid4().hex
    for version, (when, name) in enumerate(((start, "A"), (b, "B"), (c, "C"))):
        apply(
            writer, [change("teaching_course", key, when, {"display_name": name}, expected=version)]
        )
    if later in {"history", "reconfirm_B", "conflict_B"}:
        when, until, name = {
            "history": (start + timedelta(days=1), start + timedelta(days=2), "H"),
            "reconfirm_B": (c, None, "B"),
            "conflict_B": (b + timedelta(hours=1), b + timedelta(hours=2), "D"),
        }[later]
        apply(
            writer,
            [
                change(
                    "teaching_course",
                    key,
                    when,
                    {"display_name": name, **({"valid_to": until} if until else {})},
                    operation="correct",
                    expected=3,
                )
            ],
        )
    elif later == "cancel_C":
        cancel_selected(writer, key, c, 3, now)
    elif later == "gap_B":
        g1, g2 = b + timedelta(hours=1), b + timedelta(hours=2)
        apply(
            writer,
            [
                {
                    **change("teaching_course", key, g1, operation="correct", expected=3),
                    "replacement_segments": [
                        dict(effective_at=x, valid_to=y, content={"display_name": z})
                        for x, y, z in ((start, b, "A"), (b, g1, "B"), (g2, c, "B"), (c, None, "C"))
                    ],
                }
            ],
        )
    else:
        end = c + timedelta(days=1) if later == "end_C" else b + timedelta(days=1)
        original = snapshot_at(
            read_rows(writer, "teaching_course", [key], now)[0], kind="teaching_course", at=now
        )
        plan = plan_teaching_change(
            (change("teaching_course", key, end, operation="end", expected=3),),
            (original,),
            now=now,
            authorized_keys=frozenset({key}),
            complete_snapshot=True,
        )
        assert not plan.issues
        apply(writer, list(plan.changes))
    if later == "conflict_B":
        with pytest.raises(DBAPIError, match="independently changed"):
            cancel_selected(writer, key, b, 4, now)
        assert only_active(writer, "teaching_course", [key], b)[key]["display_name"] == "B"
        return
    cancel_selected(writer, key, b, 4, now)
    assert only_active(writer, "teaching_course", [key], b)[key]["display_name"] == "A"
    assert only_active(writer, "teaching_course", [key], c)[key]["display_name"] == (
        "A" if later == "cancel_C" else "B" if later == "reconfirm_B" else "C"
    )
    if later == "history":
        assert (
            only_active(writer, "teaching_course", [key], start + timedelta(days=1))[key][
                "display_name"
            ]
            == "H"
        )
    if later in {"end_B", "end_C"}:
        assert only_active(writer, "teaching_course", [key], end) == {}
    if later == "gap_B":
        assert only_active(writer, "teaching_course", [key], g1) == {}
        assert only_active(writer, "teaching_course", [key], g2)[key]["display_name"] == "A"


@pytest.mark.parametrize("unproven", ["legacy_restore", "changed_back"])
def test_unproven_overlapping_correction_remains_rejected_without_writes(writer, unproven):
    now = datetime.now(UTC)
    start, b, c = now - timedelta(days=1), now + timedelta(days=2), now + timedelta(days=4)
    key = "cancel41-unproven:" + uuid4().hex
    for version, (when, name) in enumerate(((start, "A"), (b, "B"), (c, "C"))):
        apply(
            writer, [change("teaching_course", key, when, {"display_name": name}, expected=version)]
        )
    if unproven == "legacy_restore":
        apply(
            writer,
            [
                {
                    **change("teaching_course", key, c, operation="correct", expected=3),
                    "replacement_segments": [
                        dict(effective_at=x, valid_to=y, content={"display_name": z})
                        for x, y, z in ((start, b, "A"), (b, None, "B"))
                    ],
                }
            ],
        )
        expected = 4
    else:
        for expected, name in ((3, "D"), (4, "B")):
            apply(
                writer,
                [
                    change(
                        "teaching_course",
                        key,
                        b + timedelta(hours=1),
                        {"display_name": name, "valid_to": b + timedelta(hours=2)},
                        operation="correct",
                        expected=expected,
                    )
                ],
            )
        expected = 5
    before = read_rows(writer, "teaching_course", [key], now)
    with pytest.raises(DBAPIError, match="independently changed"):
        cancel_selected(writer, key, b, expected, now)
    assert read_rows(writer, "teaching_course", [key], now) == before


@pytest.mark.parametrize("first", ["B", "C"])
@pytest.mark.parametrize("same_prefix", [False, True])
def test_both_cancellation_orders_do_not_revive_a_withdrawn_introduction(
    writer, first, same_prefix
):
    now = datetime.now(UTC)
    start, b, c = now - timedelta(days=1), now + timedelta(days=2), now + timedelta(days=4)
    key = "cancel41-order:" + uuid4().hex
    for version, (when, name) in enumerate(
        ((start, "A"), (b, "A" if same_prefix else "B"), (c, "C"))
    ):
        apply(
            writer, [change("teaching_course", key, when, {"display_name": name}, expected=version)]
        )
    cancel_selected(writer, key, b if first == "B" else c, 3, now)
    cancel_selected(writer, key, c if first == "B" else b, 4, now)
    for moment in (b, c, c + timedelta(days=1)):
        assert only_active(writer, "teaching_course", [key], moment)[key]["display_name"] == "A"


@pytest.mark.parametrize("order", [("B", "C", "D"), ("B", "D", "C")])
def test_cancelled_ancestry_preserves_original_end_even_when_a_withdrawal_restores_nothing(
    writer, order
):
    now = datetime.now(UTC)
    points = {name: now + timedelta(days=day) for name, day in (("B", 2), ("C", 4), ("D", 6))}
    key = "cancel41-ancestry:" + uuid4().hex
    end = now + timedelta(days=3)
    apply(
        writer,
        [
            change(
                "teaching_course",
                key,
                now - timedelta(days=1),
                {"display_name": "A", "valid_to": end},
            )
        ],
    )
    for version, name in enumerate(("B", "C", "D"), 1):
        apply(
            writer,
            [
                change(
                    "teaching_course", key, points[name], {"display_name": name}, expected=version
                )
            ],
        )
    for version, name in enumerate(order, 4):
        cancel_selected(writer, key, points[name], version, now)
    assert only_active(writer, "teaching_course", [key], points["B"])[key]["display_name"] == "A"
    for point in (end, points["C"], points["D"], points["D"] + timedelta(days=1)):
        assert only_active(writer, "teaching_course", [key], point) == {}


def test_formal_cause_coordinates_survive_a_cancellation_with_no_remaining_slices(writer):
    now = datetime.now(UTC)
    boundary = now + timedelta(days=1)
    key = "cancel41-empty:" + uuid4().hex
    apply(writer, [change("teaching_course", key, boundary, {"display_name": "B"})])
    cancel_selected(writer, key, boundary, 1, now)
    with writer[0]["migration"].connect() as conn:
        metadata = conn.scalar(
            text(
                "SELECT u.payload FROM master_data_import_units u "
                "JOIN school_fact_change_lineage l "
                "ON l.unit_id=u.id AND l.school_id=u.school_id WHERE l.fact_source_key=:key "
                "AND l.target_version=2"
            ),
            {"key": key},
        )
    assert (metadata.get("cause_source_key"), metadata.get("cause_version")) == (key, 1)
    assert "content" not in metadata and "replacement_segments" not in metadata


def test_self_cause_claim_cannot_disguise_an_independent_third_value_as_cancellation(writer):
    now = datetime.now(UTC)
    start, b, c = now - timedelta(days=1), now + timedelta(days=2), now + timedelta(days=4)
    key = "cancel41-false-cause:" + uuid4().hex
    for version, (when, name) in enumerate(((start, "A"), (b, "B"), (c, "C"))):
        apply(
            writer, [change("teaching_course", key, when, {"display_name": name}, expected=version)]
        )
    apply(
        writer,
        [
            {
                **change("teaching_course", key, b, operation="correct", expected=3),
                "cause_source_key": key,
                "cause_version": 2,
                "replacement_segments": [
                    dict(effective_at=x, valid_to=y, content={"display_name": z})
                    for x, y, z in ((start, b, "A"), (b, c, "G"), (c, None, "C"))
                ],
            }
        ],
    )
    before = read_rows(writer, "teaching_course", [key], now)
    with pytest.raises(DBAPIError, match="cancellation cause"):
        cancel_selected(writer, key, c, 4, now)
    assert read_rows(writer, "teaching_course", [key], now) == before


def test_cancellation_restores_independent_correct_after_an_older_equal_value_was_withdrawn(writer):
    now = datetime.now(UTC)
    a, b, correct, c = (now + timedelta(days=d) for d in (-1, 2, 3, 4))
    key = "cancel41-correct-identity:" + uuid4().hex
    apply(writer, [change("teaching_course", key, a, {"display_name": "A"})])
    apply(writer, [change("teaching_course", key, b, {"display_name": "B"}, expected=1)])
    cancel_selected(writer, key, b, 2, now)
    apply(
        writer,
        [
            change(
                "teaching_course",
                key,
                correct,
                {"display_name": "B"},
                operation="correct",
                expected=3,
            )
        ],
    )
    apply(writer, [change("teaching_course", key, c, {"display_name": "C"}, expected=4)])
    cancel_selected(writer, key, c, 5, now)
    assert only_active(writer, "teaching_course", [key], c)[key]["display_name"] == "B"
    assert (
        only_active(writer, "teaching_course", [key], c + timedelta(days=1))[key]["display_name"]
        == "B"
    )


def test_later_explicit_same_value_correct_is_an_independent_decision(writer):
    now = datetime.now(UTC)
    a, b, correction, c = (now + timedelta(days=d) for d in (-1, 2, 3, 4))
    key = "cancel41-equal-correct:" + uuid4().hex
    for version, (when, name) in enumerate(((a, "A"), (b, "B"), (c, "C"))):
        apply(
            writer, [change("teaching_course", key, when, {"display_name": name}, expected=version)]
        )
    apply(
        writer,
        [
            change(
                "teaching_course",
                key,
                correction,
                {"display_name": "B", "valid_to": c},
                operation="correct",
                expected=3,
            )
        ],
    )
    before = read_rows(writer, "teaching_course", [key], now)
    with pytest.raises(DBAPIError, match="independently changed"):
        cancel_selected(writer, key, b, 4, now)
    assert read_rows(writer, "teaching_course", [key], now) == before


def test_prior_independent_tail_deletion_requires_complete_correction(writer):
    from app.domains.students.teaching_change_planner import plan_teaching_change

    now = datetime.now(UTC)
    a, b, end, c = (now + timedelta(days=d) for d in (-1, 2, 3, 4))
    key = "cancel41-prior-stop:" + uuid4().hex
    for version, (when, name) in enumerate(((a, "A"), (b, "B"), (c, "C"))):
        apply(
            writer, [change("teaching_course", key, when, {"display_name": name}, expected=version)]
        )
    original = snapshot_at(
        read_rows(writer, "teaching_course", [key], now)[0], kind="teaching_course", at=now
    )
    plan = plan_teaching_change(
        (change("teaching_course", key, end, operation="end", expected=3),),
        (original,),
        now=now,
        authorized_keys=frozenset({key}),
        complete_snapshot=True,
    )
    assert not plan.issues
    apply(writer, list(plan.changes))
    before = read_rows(writer, "teaching_course", [key], now)
    with pytest.raises(DBAPIError, match="prior arrangement independently changed"):
        cancel_selected(writer, key, c, 4, now)
    assert read_rows(writer, "teaching_course", [key], now) == before
    assert only_active(writer, "teaching_course", [key], b)[key]["display_name"] == "B"
    assert only_active(writer, "teaching_course", [key], end) == {}
    assert only_active(writer, "teaching_course", [key], c)[key]["display_name"] == "C"
